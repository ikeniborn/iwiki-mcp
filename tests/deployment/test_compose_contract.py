from __future__ import annotations

import json
from pathlib import Path

from tests.deployment.conftest import _generate_ogg_voice


REPOSITORY = Path(__file__).parents[2]
FORBIDDEN_COMPONENTS = ("postgres", "gost", "stunnel")
STANDARD_PROXY_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}


def _mounts(service):
    mounts = service.get("volumes", [])
    return [mount for mount in mounts if isinstance(mount, dict)]


def test_rendered_compose_is_one_hardened_host_network_service(rendered_compose):
    assert set(rendered_compose["services"]) == {"iwiki"}
    service = rendered_compose["services"]["iwiki"]

    assert service["network_mode"] == "host"
    assert service["restart"] == "unless-stopped"
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert set(service["tmpfs"]) == {
        "/run:uid=10001,gid=10001,mode=0750",
        "/tmp:uid=10001,gid=10001,mode=1770",
    }
    assert set(service["security_opt"]) == {"no-new-privileges:true"}
    assert set(service["cap_drop"]) == {"ALL"}
    assert "ports" not in service
    assert "depends_on" not in service

    mounts = _mounts(service)
    config_mounts = {
        (mount["target"], mount.get("read_only", False)) for mount in mounts
    }
    assert config_mounts == {
        ("/etc/iwiki/server.toml", True),
        ("/etc/nginx/nginx.conf", True),
    }
    environment = service.get("environment", {})
    assert STANDARD_PROXY_KEYS.isdisjoint(environment)


def test_compose_and_image_sources_exclude_embedded_proxy_or_database_services(
    rendered_compose,
):
    service = rendered_compose["services"]["iwiki"]
    serialized = json.dumps(
        {
            "service_names": sorted(rendered_compose["services"]),
            "image": service.get("image", ""),
            "command": service.get("command", ""),
            "entrypoint": service.get("entrypoint", ""),
        }
    ).lower()
    assert not any(component in serialized for component in FORBIDDEN_COMPONENTS)

    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8").lower()
    assert not any(
        f" {component}" in dockerfile or f"/{component}" in dockerfile
        for component in FORBIDDEN_COMPONENTS
    )


def test_built_image_has_exact_runtime_executable_inventory(
    docker_command, acceptance_image
):
    import subprocess

    inspect = subprocess.run(
        [
            *docker_command,
            "image",
            "inspect",
            acceptance_image,
            "--format",
            "{{.Config.User}}",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert inspect.stdout.strip() == "10001:10001"

    inventory = subprocess.run(
        [
            *docker_command,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            acceptance_image,
            "-c",
            "for x in iwiki-mcp iwiki-telegram-bot nginx supervisord ffmpeg "
            "postgres gost stunnel; do command -v \"$x\" >/dev/null "
            "&& echo \"$x\"; done; true",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert set(inventory.stdout.splitlines()) == {
        "iwiki-mcp",
        "iwiki-telegram-bot",
        "nginx",
        "supervisord",
        "ffmpeg",
    }


def test_built_image_generates_marker_bearing_ogg_voice(
    docker_command, acceptance_image
):
    marker = "audio-privacy-marker"

    audio = _generate_ogg_voice(
        acceptance_image, docker_command, marker
    )

    assert audio.startswith(b"OggS")
    assert marker.encode() in audio
