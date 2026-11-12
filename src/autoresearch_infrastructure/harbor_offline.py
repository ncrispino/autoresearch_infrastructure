"""Keep a no-network Harbor task offline on hosts where Harbor cannot control egress dynamically."""

from pathlib import Path

from harbor.environments.docker.docker import DockerEnvironment


class OfflineDockerEnvironment(DockerEnvironment):
    """A Docker environment that can always disable the network. It adds nothing else."""

    @property
    def capabilities(self):
        return super().capabilities.model_copy(update={"disable_internet": True})

    @property
    def _docker_compose_paths(self):
        paths = super()._docker_compose_paths
        if not self._enable_egress_control:
            # Harbor ships this Compose file. Adding it turns the network off
            # statically when dynamic egress control is unavailable.
            from harbor.environments.docker import docker

            paths.append(Path(docker.__file__).parent / "docker-compose-no-network.yaml")
        return paths
