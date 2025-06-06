import os
import asyncio
from functools import lru_cache
from typing import Callable, Dict, Any
from uuid import UUID

import docker
import httpx
import tenacity
from docker.models.containers import Container

from openhands.core.config import AppConfig
from openhands.core.exceptions import (
    AgentRuntimeDisconnectedError,
    AgentRuntimeNotFoundError,
)
from openhands.core.logger import DEBUG, DEBUG_RUNTIME
from openhands.core.logger import openhands_logger as logger
from openhands.events import EventStream
from openhands.runtime.builder import DockerRuntimeBuilder
from openhands.runtime.impl.action_execution.action_execution_client import (
    ActionExecutionClient,
)
from openhands.runtime.impl.docker.containers import stop_all_containers
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.utils import find_available_tcp_port
from openhands.runtime.utils.command import get_action_execution_server_startup_command
from openhands.runtime.utils.log_streamer import LogStreamer
from openhands.runtime.utils.runtime_build import build_runtime_image
from openhands.utils.async_utils import call_sync_from_async
from openhands.utils.shutdown_listener import add_shutdown_listener
from openhands.utils.tenacity_stop import stop_if_should_exit

CONTAINER_NAME_PREFIX = 'openhands-runtime-'

EXECUTION_SERVER_PORT_RANGE = (30000, 39999)
VSCODE_PORT_RANGE = (40000, 49999)
APP_PORT_RANGE_1 = (50000, 54999)
APP_PORT_RANGE_2 = (55000, 59999)


def _is_retryable_wait_until_alive_error(exception):
    if isinstance(exception, tenacity.RetryError):
        cause = exception.last_attempt.exception()
        return _is_retryable_wait_until_alive_error(cause)

    return isinstance(
        exception,
        (
            ConnectionError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
            httpx.ConnectTimeout,  # Added ConnectTimeout explicitly
            docker.errors.NotFound,  # Added NotFound for container startup race conditions
        ),
    )


class DockerRuntime(ActionExecutionClient):
    """This runtime will subscribe the event stream.

    When receive an event, it will send the event to runtime-client which run inside the docker environment.

    Args:
        config (AppConfig): The application configuration.
        event_stream (EventStream): The event stream to subscribe to.
        sid (str, optional): The session ID. Defaults to 'default'.
        plugins (list[PluginRequirement] | None, optional): List of plugin requirements. Defaults to None.
        env_vars (dict[str, str] | None, optional): Environment variables to set. Defaults to None.
    """

    _shutdown_listener_id: UUID | None = None

    def __init__(
        self,
        config: AppConfig,
        event_stream: EventStream,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Callable | None = None,
        attach_to_existing: bool = False,
        headless_mode: bool = True,
    ):
        if not DockerRuntime._shutdown_listener_id:
            DockerRuntime._shutdown_listener_id = add_shutdown_listener(
                lambda: stop_all_containers(CONTAINER_NAME_PREFIX)
            )

        self.config = config
        self.status_callback = status_callback

        self._host_port = -1
        self._container_port = -1
        self._vscode_port = -1
        self._app_ports: list[int] = []

        # Set default local runtime URL if not specified
        if not os.environ.get('DOCKER_HOST_ADDR'):
            # Try to determine the best local address for Docker communication
            if os.name == 'nt':  # Windows
                self.config.sandbox.local_runtime_url = 'http://host.docker.internal'
            else:
                # For Linux/Mac, try to get the Docker bridge network gateway
                try:
                    bridge_ip = self.docker_client.networks.get('bridge').attrs['IPAM']['Config'][0]['Gateway']
                    self.config.sandbox.local_runtime_url = f'http://{bridge_ip}'
                except Exception:
                    # Fallback to localhost/127.0.0.1 if bridge network info not available
                    self.config.sandbox.local_runtime_url = 'http://127.0.0.1'
        else:
            logger.info(
                f'Using DOCKER_HOST_IP: {os.environ["DOCKER_HOST_ADDR"]} for local_runtime_url'
            )
            self.config.sandbox.local_runtime_url = (
                f'http://{os.environ["DOCKER_HOST_ADDR"]}'
            )

        self.docker_client: docker.DockerClient = self._init_docker_client()
        self.api_url = f'{self.config.sandbox.local_runtime_url}:{self._container_port}'

        self.base_container_image = self.config.sandbox.base_container_image
        self.runtime_container_image = self.config.sandbox.runtime_container_image
        self.container_name = CONTAINER_NAME_PREFIX + sid
        self.container: Container | None = None

        self.runtime_builder = DockerRuntimeBuilder(self.docker_client)

        # Buffer for container logs
        self.log_streamer: LogStreamer | None = None

        super().__init__(
            config,
            event_stream,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
            headless_mode,
        )

        # Log runtime_extra_deps after base class initialization so self.sid is available
        if self.config.sandbox.runtime_extra_deps:
            self.log(
                'debug',
                f'Installing extra user-provided dependencies in the runtime image: {self.config.sandbox.runtime_extra_deps}',
            )

    @property
    def action_execution_server_url(self):
        return self.api_url

    async def connect(self):
        self.send_status_message('STATUS$STARTING_RUNTIME')
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                await call_sync_from_async(self._attach_to_container)
                break
            except (docker.errors.NotFound, httpx.ConnectTimeout) as e:
                retry_count += 1
                if retry_count >= max_retries or self.attach_to_existing:
                    if self.attach_to_existing:
                        self.log(
                            'error',
                            f'Container {self.container_name} not found.',
                        )
                        raise AgentRuntimeDisconnectedError from e

                    # Initialize new container if needed
                    if self.runtime_container_image is None:
                        if self.base_container_image is None:
                            raise ValueError(
                                'Neither runtime container image nor base container image is set'
                            )
                        self.send_status_message('STATUS$STARTING_CONTAINER')
                        self.runtime_container_image = build_runtime_image(
                            self.base_container_image,
                            self.runtime_builder,
                            platform=self.config.sandbox.platform,
                            extra_deps=self.config.sandbox.runtime_extra_deps,
                            force_rebuild=self.config.sandbox.force_rebuild_runtime,
                            extra_build_args=self.config.sandbox.runtime_extra_build_args,
                        )

                    self.log(
                        'info', f'Starting runtime with image: {self.runtime_container_image}'
                    )
                    await call_sync_from_async(self._init_container)
                    self.log(
                        'info',
                        f'Container started: {self.container_name}. VSCode URL: {self.vscode_url}',
                    )
                else:
                    self.log('warning', f'Connection attempt {retry_count} failed, retrying...')
                    await asyncio.sleep(2 ** retry_count)  # Exponential backoff

        if DEBUG_RUNTIME:
            self.log_streamer = LogStreamer(self.container, self.log)
        else:
            self.log_streamer = None

        if not self.attach_to_existing:
            self.log('info', f'Waiting for client to become ready at {self.api_url}...')
            self.send_status_message('STATUS$WAITING_FOR_CLIENT')

        await call_sync_from_async(self._wait_until_alive)

        if not self.attach_to_existing:
            self.log('info', 'Runtime is ready.')

        if not self.attach_to_existing:
            await call_sync_from_async(self.setup_initial_env)

        self.log(
            'debug',
            f'Container initialized with plugins: {[plugin.name for plugin in self.plugins]}. VSCode URL: {self.vscode_url}',
        )
        if not self.attach_to_existing:
            self.send_status_message(' ')
        self._runtime_initialized = True

    @staticmethod
    @lru_cache(maxsize=1)
    def _init_docker_client() -> docker.DockerClient:
        try:
            return docker.from_env()
        except Exception as ex:
            logger.error(
                'Launch docker client failed. Please make sure you have installed docker and started docker desktop/daemon.',
            )
            raise ex

    @tenacity.retry(
        stop=tenacity.stop_after_delay(180) | stop_if_should_exit(),
        retry=tenacity.retry_if_exception(_is_retryable_wait_until_alive_error),
        reraise=True,
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
    )
    def _wait_until_alive(self):
        try:
            container = self.docker_client.containers.get(self.container_name)
            if container.status == 'exited':
                logs = container.logs(tail=50).decode('utf-8')
                raise AgentRuntimeDisconnectedError(
                    f'Container {self.container_name} has exited. Last logs:\n{logs}'
                )
            
            # Check container network settings and DNS resolution
            network_settings = container.attrs.get('NetworkSettings', {})
            if not network_settings.get('IPAddress'):
                # Try to ping the container to verify connectivity
                try:
                    container.exec_run('ping -c 1 host.docker.internal', privileged=True)
                    self.log('debug', 'Successfully verified host.docker.internal DNS resolution')
                except Exception as e:
                    self.log('warning', f'DNS resolution test failed: {str(e)}. Using fallback configuration.')
                    # Update API URL to use container IP if available
                    container_ip = network_settings.get('Gateway')
                    if container_ip:
                        self.api_url = f'http://{container_ip}:{self._container_port}'
                        self.log('debug', f'Updated API URL to use container gateway: {self.api_url}')

            self.log('debug', f'Attempting to connect to runtime at {self.api_url}')
            self.check_if_alive()
            
        except docker.errors.NotFound:
            raise AgentRuntimeNotFoundError(
                f'Container {self.container_name} not found.'
            )
        except Exception as e:
            self.log('error', f'Error checking container status: {str(e)}')
            raise

    def _init_container(self):
        self.log('debug', 'Preparing to start container...')
        self.send_status_message('STATUS$PREPARING_CONTAINER')
        
        # Determine optimal network mode based on platform and Docker version
        use_host_network = self.config.sandbox.use_host_network
        network_mode = None
        
        if use_host_network:
            network_mode = 'host'
        else:
            # Check if we're on Docker Desktop which supports host.docker.internal
            try:
                version_info = self.docker_client.version()
                is_docker_desktop = any('docker-desktop' in component.get('Name', '').lower() 
                                     for component in version_info.get('Components', []))
                
                if is_docker_desktop:
                    # Docker Desktop supports host.docker.internal out of the box
                    network_mode = 'bridge'
                else:
                    # For other Docker installations, we need to handle host resolution
                    network_mode = 'bridge'
                    extra_hosts = {'host.docker.internal': 'host-gateway'}
                
            except Exception as e:
                self.log('warning', f'Failed to detect Docker environment: {str(e)}. Using default bridge network.')
                network_mode = 'bridge'

        # Initialize port mappings with improved error handling
        port_mapping: dict[str, list[dict[str, str]]] | None = None
        try:
            self._host_port = self._find_available_port(EXECUTION_SERVER_PORT_RANGE)
            self._container_port = self._host_port
            self._vscode_port = self._find_available_port(VSCODE_PORT_RANGE)
            self._app_ports = [
                self._find_available_port(APP_PORT_RANGE_1),
                self._find_available_port(APP_PORT_RANGE_2),
            ]
        except Exception as e:
            self.log('error', f'Failed to allocate ports: {str(e)}')
            raise

        # Verify host.docker.internal resolution before proceeding
        try:
            # Try to resolve the container's hostname first
            hostname = None
            if os.name == 'nt':  # Windows
                hostname = 'host.docker.internal'
            else:
                hostname = self.config.sandbox.local_runtime_url.split('://')[-1].split(':')[0]
            
            self.log('debug', f'Using hostname for container communication: {hostname}')
            self.api_url = f'http://{hostname}:{self._container_port}'
        except Exception as e:
            self.log('warning', f'Failed to determine container hostname: {str(e)}, falling back to localhost')
            self.api_url = f'http://localhost:{self._container_port}'

        if not use_host_network:
            # Ensure we bind to localhost first for better security and accessibility
            bind_address = self.config.sandbox.runtime_binding_address or '127.0.0.1'
            port_mapping = {
                f'{self._container_port}/tcp': [
                    {
                        'HostPort': str(self._host_port),
                        'HostIp': bind_address,
                    }
                ],
            }

            if self.vscode_enabled:
                port_mapping[f'{self._vscode_port}/tcp'] = [
                    {
                        'HostPort': str(self._vscode_port),
                        'HostIp': bind_address,
                    }
                ]

            for port in self._app_ports:
                port_mapping[f'{port}/tcp'] = [
                    {
                        'HostPort': str(port),
                        'HostIp': bind_address,
                    }
                ]
        else:
            self.log(
                'warn',
                'Using host network mode. If you are using MacOS, please make sure you have the latest version of Docker Desktop and enabled host network feature: https://docs.docker.com/network/drivers/host/#docker-desktop',
            )

        # Combine environment variables with improved networking configuration
        environment = {
            'port': str(self._container_port),
            'PYTHONUNBUFFERED': '1',
            'VSCODE_PORT': str(self._vscode_port),
            'PIP_BREAK_SYSTEM_PACKAGES': '1',
            'HOST_HOSTNAME': 'host.docker.internal',  # Ensure host hostname is set
            'DOCKER_DEFAULT_PLATFORM': self.config.sandbox.platform or 'linux/amd64',
            'DOCKER_BUILDKIT': '1',
            'DOCKER_DNS': '8.8.8.8',  # Fallback DNS server
        }
        
        if self.config.debug or DEBUG:
            environment['DEBUG'] = 'true'
            environment['DOCKER_BUILDKIT_PROGRESS'] = 'plain'
            
        # also update with runtime_startup_env_vars
        environment.update(self.config.sandbox.runtime_startup_env_vars)

        self.log('debug', f'Workspace Base: {self.config.workspace_base}')
        if (
            self.config.workspace_mount_path is not None
            and self.config.workspace_mount_path_in_sandbox is not None
        ):
            # e.g. result would be: {"/home/user/openhands/workspace": {'bind': "/workspace", 'mode': 'rw'}}
            volumes = {
                self.config.workspace_mount_path: {
                    'bind': self.config.workspace_mount_path_in_sandbox,
                    'mode': 'rw',
                }
            }
            logger.debug(f'Mount dir: {self.config.workspace_mount_path}')
        else:
            logger.debug(
                'Mount dir is not set, will not mount the workspace directory to the container'
            )
            volumes = None
        self.log(
            'debug',
            f'Sandbox workspace: {self.config.workspace_mount_path_in_sandbox}',
        )

        command = get_action_execution_server_startup_command(
            server_port=self._container_port,
            plugins=self.plugins,
            app_config=self.config,
        )

        try:
            self.container = self.docker_client.containers.run(
                self.runtime_container_image,
                command=command,
                # Override the default 'bash' entrypoint because the command is a binary.
                entrypoint=[],
                network_mode=network_mode,
                ports=port_mapping,
                working_dir='/openhands/code/',  # do not change this!
                name=self.container_name,
                detach=True,
                environment=environment,
                volumes=volumes,
                extra_hosts=extra_hosts,  # Add host mapping
                device_requests=(
                    [docker.types.DeviceRequest(capabilities=[['gpu']], count=-1)]
                    if self.config.sandbox.enable_gpu
                    else None
                ),
                **(self.config.sandbox.docker_runtime_kwargs or {}),
            )
            self.log('debug', f'Container started. Server url: {self.api_url}')
            
            # Verify container networking
            if not network_mode == 'host':
                container_info = self.container.attrs
                container_ip = container_info['NetworkSettings']['IPAddress']
                self.log('debug', f'Container IP: {container_ip}')
                
            self.send_status_message('STATUS$CONTAINER_STARTED')
            
        except docker.errors.APIError as e:
            if '409' in str(e):
                self.log(
                    'warning',
                    f'Container {self.container_name} already exists. Removing...',
                )
                stop_all_containers(self.container_name)
                return self._init_container()

            else:
                self.log(
                    'error',
                    f'Error: Instance {self.container_name} FAILED to start container!\n',
                )
                self.log('error', str(e))
                raise e
        except Exception as e:
            self.log(
                'error',
                f'Error: Instance {self.container_name} FAILED to start container!\n',
            )
            self.log('error', str(e))
            self.close()
            raise e

    def _attach_to_container(self):
        self.container = self.docker_client.containers.get(self.container_name)
        if self.container.status == 'exited':
            self.container.start()

        config = self.container.attrs['Config']
        for env_var in config['Env']:
            if env_var.startswith('port='):
                self._host_port = int(env_var.split('port=')[1])
                self._container_port = self._host_port
            elif env_var.startswith('VSCODE_PORT='):
                self._vscode_port = int(env_var.split('VSCODE_PORT=')[1])

        self._app_ports = []
        exposed_ports = config.get('ExposedPorts')
        if exposed_ports:
            for exposed_port in exposed_ports.keys():
                exposed_port = int(exposed_port.split('/tcp')[0])
                if (
                    exposed_port != self._host_port
                    and exposed_port != self._vscode_port
                ):
                    self._app_ports.append(exposed_port)

        self.api_url = f'{self.config.sandbox.local_runtime_url}:{self._container_port}'
        self.log(
            'debug',
            f'attached to container: {self.container_name} {self._container_port} {self.api_url}',
        )

    def close(self, rm_all_containers: bool | None = None):
        """Closes the DockerRuntime and associated objects

        Parameters:
        - rm_all_containers (bool): Whether to remove all containers with the 'openhands-sandbox-' prefix
        """
        super().close()
        if self.log_streamer:
            self.log_streamer.close()

        if rm_all_containers is None:
            rm_all_containers = self.config.sandbox.rm_all_containers

        if self.config.sandbox.keep_runtime_alive or self.attach_to_existing:
            return
        close_prefix = (
            CONTAINER_NAME_PREFIX if rm_all_containers else self.container_name
        )
        stop_all_containers(close_prefix)

    def _is_port_in_use_docker(self, port):
        containers = self.docker_client.containers.list()
        for container in containers:
            container_ports = container.ports
            if str(port) in str(container_ports):
                return True
        return False

    def _find_available_port(self, port_range, max_attempts=5):
        port = port_range[1]
        for _ in range(max_attempts):
            port = find_available_tcp_port(port_range[0], port_range[1])
            if not self._is_port_in_use_docker(port):
                return port
        # If no port is found after max_attempts, return the last tried port
        return port

    @property
    def vscode_url(self) -> str | None:
        token = super().get_vscode_token()
        if not token:
            return None

        vscode_url = f'http://localhost:{self._vscode_port}/?tkn={token}&folder={self.config.workspace_mount_path_in_sandbox}'
        return vscode_url

    @property
    def web_hosts(self):
        hosts: dict[str, int] = {}

        for port in self._app_ports:
            hosts[f'http://localhost:{port}'] = port

        return hosts

    def pause(self):
        """Pause the runtime by stopping the container.
        This is different from container.stop() as it ensures environment variables are properly preserved."""
        if not self.container:
            raise RuntimeError('Container not initialized')

        # First, ensure all environment variables are properly persisted in .bashrc
        # This is already handled by add_env_vars in base.py

        # Stop the container
        self.container.stop()
        self.log('debug', f'Container {self.container_name} paused')

    def resume(self):
        """Resume the runtime by starting the container.
        This is different from container.start() as it ensures environment variables are properly restored."""
        if not self.container:
            raise RuntimeError('Container not initialized')

        # Start the container
        self.container.start()
        self.log('debug', f'Container {self.container_name} resumed')

        # Wait for the container to be ready
        self._wait_until_alive()

    @classmethod
    async def delete(cls, conversation_id: str):
        docker_client = cls._init_docker_client()
        try:
            container_name = CONTAINER_NAME_PREFIX + conversation_id
            container = docker_client.containers.get(container_name)
            container.remove(force=True)
        except docker.errors.APIError:
            pass
        except docker.errors.NotFound:
            pass
        finally:
            docker_client.close()
