# Copyright 2020 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Provides an interface to xDS Test Client running remotely.
"""
import datetime
import functools
import logging
from typing import Iterable, List, Optional

import framework.errors
from framework.helpers import retryers
import framework.rpc
from framework.rpc import grpc_channelz
from framework.rpc import grpc_csds
from framework.rpc import grpc_testing

logger = logging.getLogger(__name__)

# Type aliases
_timedelta = datetime.timedelta
_LoadBalancerStatsServiceClient = grpc_testing.LoadBalancerStatsServiceClient
_XdsUpdateClientConfigureServiceClient = (
    grpc_testing.XdsUpdateClientConfigureServiceClient
)
_ChannelzServiceClient = grpc_channelz.ChannelzServiceClient
_ChannelzChannel = grpc_channelz.Channel
_ChannelzChannelState = grpc_channelz.ChannelState
_ChannelzSubchannel = grpc_channelz.Subchannel
_ChannelzSocket = grpc_channelz.Socket
_CsdsClient = grpc_csds.CsdsClient

# Use in get_load_balancer_stats request to request all metadata.
REQ_LB_STATS_METADATA_ALL = ("*",)

DEFAULT_TD_XDS_URI = "trafficdirector.googleapis.com:443"


class XdsTestClient(framework.rpc.grpc.GrpcApp):
    """
    Represents RPC services implemented in Client component of the xds test app.
    https://github.com/grpc/grpc/blob/master/doc/xds-test-descriptions.md#client
    """

    # A unique string identifying each client replica. Used in logging.
    hostname: str

    def __init__(
        self,
        *,
        ip: str,
        rpc_port: int,
        server_target: str,
        hostname: str,
        rpc_host: Optional[str] = None,
        maintenance_port: Optional[int] = None,
    ):
        super().__init__(rpc_host=(rpc_host or ip))
        self.ip = ip
        self.rpc_port = rpc_port
        self.server_target = server_target
        self.maintenance_port = maintenance_port or rpc_port
        self.hostname = hostname

    @property
    @functools.lru_cache(None)
    def load_balancer_stats(self) -> _LoadBalancerStatsServiceClient:
        return _LoadBalancerStatsServiceClient(
            self._make_channel(self.rpc_port),
            log_target=f"{self.hostname}:{self.rpc_port}",
        )

    @property
    @functools.lru_cache(None)
    def update_config(self):
        return _XdsUpdateClientConfigureServiceClient(
            self._make_channel(self.rpc_port),
            log_target=f"{self.hostname}:{self.rpc_port}",
        )

    @property
    @functools.lru_cache(None)
    def channelz(self) -> _ChannelzServiceClient:
        return _ChannelzServiceClient(
            self._make_channel(self.maintenance_port),
            log_target=f"{self.hostname}:{self.maintenance_port}",
        )

    @property
    @functools.lru_cache(None)
    def csds(self) -> _CsdsClient:
        return _CsdsClient(
            self._make_channel(self.maintenance_port),
            log_target=f"{self.hostname}:{self.maintenance_port}",
        )

    def get_load_balancer_stats(
        self,
        *,
        num_rpcs: int,
        metadata_keys: Optional[tuple[str, ...]] = None,
        timeout_sec: Optional[int] = None,
    ) -> grpc_testing.LoadBalancerStatsResponse:
        """
        Shortcut to LoadBalancerStatsServiceClient.get_client_stats()
        """
        return self.load_balancer_stats.get_client_stats(
            num_rpcs=num_rpcs,
            timeout_sec=timeout_sec,
            metadata_keys=metadata_keys,
        )

    def get_load_balancer_accumulated_stats(
        self,
        *,
        timeout_sec: Optional[int] = None,
    ) -> grpc_testing.LoadBalancerAccumulatedStatsResponse:
        """Shortcut to LoadBalancerStatsServiceClient.get_client_accumulated_stats()"""
        return self.load_balancer_stats.get_client_accumulated_stats(
            timeout_sec=timeout_sec
        )

    def wait_for_server_channel_ready(
        self,
        *,
        timeout: Optional[_timedelta] = None,
        rpc_deadline: Optional[_timedelta] = None,
    ) -> _ChannelzChannel:
        """Wait for the channel to the server to transition to READY.

        Raises:
            GrpcApp.NotFound: If the channel never transitioned to READY.
        """
        try:
            return self.wait_for_server_channel_state(
                _ChannelzChannelState.READY,
                timeout=timeout,
                rpc_deadline=rpc_deadline,
            )
        except retryers.RetryError as retry_err:
            if isinstance(retry_err.exception(), self.ChannelNotFound):
                retry_err.add_note(
                    framework.errors.FrameworkError.note_blanket_error(
                        "The client couldn't connect to the server."
                    )
                )
            raise

    def wait_for_active_xds_channel(
        self,
        *,
        xds_server_uri: Optional[str] = None,
        timeout: Optional[_timedelta] = None,
        rpc_deadline: Optional[_timedelta] = None,
    ) -> _ChannelzChannel:
        """Wait until the xds channel is active or timeout.

        Raises:
            GrpcApp.NotFound: If the channel to xds never transitioned to active.
        """
        try:
            return self.wait_for_xds_channel_active(
                xds_server_uri=xds_server_uri,
                timeout=timeout,
                rpc_deadline=rpc_deadline,
            )
        except retryers.RetryError as retry_err:
            if isinstance(retry_err.exception(), self.ChannelNotFound):
                retry_err.add_note(
                    framework.errors.FrameworkError.note_blanket_error(
                        "The client couldn't connect to the xDS control plane."
                    )
                )
            raise

    def get_active_server_channel_socket(self) -> _ChannelzSocket:
        channel = self.find_server_channel_with_state(
            _ChannelzChannelState.READY
        )
        # Get the first subchannel of the active channel to the server.
        logger.debug(
            (
                "[%s] Retrieving client -> server socket, "
                "channel_id: %s, subchannel: %s"
            ),
            self.hostname,
            channel.ref.channel_id,
            channel.subchannel_ref[0].name,
        )
        subchannel, *subchannels = list(
            self.channelz.list_channel_subchannels(channel)
        )
        if subchannels:
            logger.warning(
                "[%s] Unexpected subchannels: %r", self.hostname, subchannels
            )
        # Get the first socket of the subchannel
        socket, *sockets = list(
            self.channelz.list_subchannels_sockets(subchannel)
        )
        if sockets:
            logger.warning(
                "[%s] Unexpected sockets: %r", self.hostname, subchannels
            )
        logger.debug(
            "[%s] Found client -> server socket: %s",
            self.hostname,
            socket.ref.name,
        )
        return socket

    def wait_for_server_channel_state(
        self,
        state: _ChannelzChannelState,
        *,
        timeout: Optional[_timedelta] = None,
        rpc_deadline: Optional[_timedelta] = None,
    ) -> _ChannelzChannel:
        # When polling for a state, prefer smaller wait times to avoid
        # exhausting all allowed time on a single long RPC.
        if rpc_deadline is None:
            rpc_deadline = _timedelta(seconds=30)

        # Fine-tuned to wait for the channel to the server.
        retryer = retryers.exponential_retryer_with_timeout(
            wait_min=_timedelta(seconds=10),
            wait_max=_timedelta(seconds=25),
            timeout=_timedelta(minutes=5) if timeout is None else timeout,
        )

        logger.info(
            "[%s] Waiting to report a %s channel to %s",
            self.hostname,
            _ChannelzChannelState.Name(state),
            self.server_target,
        )
        channel = retryer(
            self.find_server_channel_with_state,
            state,
            rpc_deadline=rpc_deadline,
        )
        logger.info(
            "[%s] Channel to %s transitioned to state %s: %s",
            self.hostname,
            self.server_target,
            _ChannelzChannelState.Name(state),
            _ChannelzServiceClient.channel_repr(channel),
        )
        return channel

    def wait_for_xds_channel_active(
        self,
        *,
        xds_server_uri: Optional[str] = None,
        timeout: Optional[_timedelta] = None,
        rpc_deadline: Optional[_timedelta] = None,
    ) -> _ChannelzChannel:
        if not xds_server_uri:
            xds_server_uri = DEFAULT_TD_XDS_URI
        # When polling for a state, prefer smaller wait times to avoid
        # exhausting all allowed time on a single long RPC.
        if rpc_deadline is None:
            rpc_deadline = _timedelta(seconds=30)

        retryer = retryers.exponential_retryer_with_timeout(
            wait_min=_timedelta(seconds=10),
            wait_max=_timedelta(seconds=25),
            timeout=_timedelta(minutes=5) if timeout is None else timeout,
        )

        logger.info(
            "[%s] ADS: Waiting for successful calls to xDS control plane to %s",
            self.hostname,
            xds_server_uri,
        )
        channel = retryer(
            self.find_active_xds_channel,
            xds_server_uri=xds_server_uri,
            rpc_deadline=rpc_deadline,
        )
        logger.info(
            "[%s] ADS: Detected successful calls to xDS control plane %s",
            self.hostname,
            xds_server_uri,
        )
        return channel

    def find_active_xds_channel(
        self,
        xds_server_uri: str,
        *,
        rpc_deadline: Optional[_timedelta] = None,
    ) -> _ChannelzChannel:
        rpc_params = {}
        if rpc_deadline is not None:
            rpc_params["deadline_sec"] = rpc_deadline.total_seconds()

        for channel in self.get_server_channels(xds_server_uri, **rpc_params):
            logger.info(
                "[%s] xDS control plane channel: %s",
                self.hostname,
                _ChannelzServiceClient.channel_repr(channel),
            )

            try:
                channel = self.check_channel_successful_calls(
                    channel, **rpc_params
                )
                logger.info(
                    "[%s] Detected successful calls to xDS control plane: %s",
                    self.hostname,
                    xds_server_uri,
                )
            except self.NotFound as e:
                # Otherwise, keep searching.
                continue

            return channel

        raise self.ChannelNotActive(
            f"[{self.hostname}] Client has no"
            f" active channel with xds server {xds_server_uri}",
            src=self.hostname,
            dst=xds_server_uri,
        )

    def find_server_channel_with_state(
        self,
        expected_state: _ChannelzChannelState,
        *,
        rpc_deadline: Optional[_timedelta] = None,
        check_subchannel=True,
    ) -> _ChannelzChannel:
        rpc_params = {}
        if rpc_deadline is not None:
            rpc_params["deadline_sec"] = rpc_deadline.total_seconds()

        expected_state_name: str = _ChannelzChannelState.Name(expected_state)
        target: str = self.server_target

        for channel in self.get_server_channels(target, **rpc_params):
            channel_state: _ChannelzChannelState = channel.data.state.state
            logger.info(
                "[%s] Server channel: %s",
                self.hostname,
                _ChannelzServiceClient.channel_repr(channel),
            )
            if channel_state is expected_state:
                if check_subchannel:
                    # When requested, check if the channel has at least
                    # one subchannel in the requested state.
                    try:
                        subchannel = self.find_subchannel_with_state(
                            channel, expected_state, **rpc_params
                        )
                        logger.info(
                            "[%s] Found subchannel in state %s: %s",
                            self.hostname,
                            expected_state_name,
                            _ChannelzServiceClient.subchannel_repr(subchannel),
                        )
                    except self.NotFound as e:
                        # Otherwise, keep searching.
                        logger.info(e.message)
                        continue
                return channel

        raise self.ChannelNotFound(
            f"[{self.hostname}] Client has no"
            f" {expected_state_name} channel with server {target}",
            src=self.hostname,
            dst=target,
            expected_state=expected_state,
        )

    def get_server_channels(
        self, server_target: str, **kwargs
    ) -> Iterable[_ChannelzChannel]:
        return self.channelz.find_channels_for_target(server_target, **kwargs)

    def find_subchannel_with_state(
        self, channel: _ChannelzChannel, state: _ChannelzChannelState, **kwargs
    ) -> _ChannelzSubchannel:
        subchannels = self.channelz.list_channel_subchannels(channel, **kwargs)
        for subchannel in subchannels:
            if subchannel.data.state.state is state:
                return subchannel

        raise self.NotFound(
            f"[{self.hostname}] Not found "
            f"a {_ChannelzChannelState.Name(state)} subchannel "
            f"for channel_id {channel.ref.channel_id}"
        )

    def find_subchannels_with_state(
        self, state: _ChannelzChannelState, **kwargs
    ) -> List[_ChannelzSubchannel]:
        subchannels = []
        for channel in self.channelz.find_channels_for_target(
            self.server_target, **kwargs
        ):
            for subchannel in self.channelz.list_channel_subchannels(
                channel, **kwargs
            ):
                if subchannel.data.state.state is state:
                    subchannels.append(subchannel)
        return subchannels

    def check_channel_successful_calls(
        self, channel: _ChannelzChannel, **kwargs
    ) -> _ChannelzChannel:
        """Checks if the channel has any successful calls.

        We consider the channel is active if channel is in READY state and calls_started is
        greater than calls_failed.
        """
        if (
            channel.data.state.state is _ChannelzChannelState.READY
            and channel.data.calls_started > channel.data.calls_failed
        ):
            return channel

        raise self.NotFound(
            f"[{self.hostname}] Not found successful calls over the channel."
        )

    class ChannelNotFound(framework.rpc.grpc.GrpcApp.NotFound):
        """Channel with expected status not found"""

        src: str
        dst: str
        expected_state: object

        def __init__(
            self,
            message: str,
            *,
            src: str,
            dst: str,
            expected_state: _ChannelzChannelState,
            **kwargs,
        ):
            self.src = src
            self.dst = dst
            self.expected_state = expected_state
            super().__init__(message, src, dst, expected_state, **kwargs)

    class ChannelNotActive(framework.rpc.grpc.GrpcApp.NotFound):
        """No active channel was found"""

        src: str
        dst: str

        def __init__(
            self,
            message: str,
            *,
            src: str,
            dst: str,
            **kwargs,
        ):
            self.src = src
            self.dst = dst
            super().__init__(message, src, dst, **kwargs)
