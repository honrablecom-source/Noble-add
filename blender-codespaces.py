#!/usr/bin/env python3
"""Install and run the browser-streamed Blender desktop in GitHub Codespaces."""

import fcntl
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".local/state/codespace-blender"
PID = STATE / "desktop.pid"
DISPLAY = ":99"
PORT = 6080
ENV = dict(
    os.environ,
    DISPLAY=DISPLAY,
    LIBGL_ALWAYS_SOFTWARE="1",
    GALLIUM_DRIVER="llvmpipe",
    LP_NUM_THREADS="2",
    OMP_NUM_THREADS="2",
)
PACKAGES = (
    "blender xvfb x11vnc novnc websockify openbox xterm mesa-utils "
    "x11-utils xdotool dbus-x11 fonts-dejavu-core python3-venv"
).split()
BLENDER_PROFILE = """
import bpy, os, runpy
root = os.environ["BLENDER_CODESPACES_ROOT"]
prefs = bpy.context.preferences
prefs.view.show_splash = False
prefs.inputs.use_mouse_emulate_3_button = True
prefs.inputs.use_emulate_numpad = True
prefs.edit.undo_steps = 32
prefs.edit.undo_memory_limit = 256
prefs.filepaths.use_auto_save_temporary_files = True
prefs.filepaths.auto_save_time = 2
recovery = os.path.join(root, "blender-projects", "recovery")
os.makedirs(recovery, exist_ok=True)
prefs.filepaths.temporary_directory = recovery
for scene in bpy.data.scenes:
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 2
    scene.render.resolution_percentage = 50
    if hasattr(scene, "cycles"):
        scene.cycles.device = "CPU"
        scene.cycles.samples = 32
        scene.cycles.use_denoising = True
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.shading.type = "SOLID"
addon = os.path.expanduser("~/.config/blender/4.0/scripts/addons/blender_mcp.py")
if os.path.isfile(addon):
    runpy.run_path(os.path.join(root, "scripts", "enable-blender-mcp.py"))
print("Codespaces Blender profile loaded.", flush=True)
"""


def desktop_url():
    name = os.environ.get("CODESPACE_NAME")
    domain = os.environ.get(
        "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev"
    )
    base = f"https://{name}-{PORT}.{domain}" if name else f"http://localhost:{PORT}"
    return base + "/vnc.html?autoconnect=true&resize=scale&path=websockify"


def supervisor_pid():
    try:
        pid = int(PID.read_text())
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        if (
            b"--serve" in command
            and (b"blender-codespaces.py" in command or b"blender-desktop" in command)
        ):
            return pid
    except (OSError, ValueError):
        pass
    return None


def install():
    subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True)
    subprocess.run(
        [
            "sudo",
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *PACKAGES,
        ],
        check=True,
    )


def serve():
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / "desktop.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return

    children = []
    logs = []

    def spawn(name, args):
        log = (STATE / f"{name}.log").open("a")
        logs.append(log)
        process = subprocess.Popen(args, env=ENV, cwd=ROOT, stdout=log, stderr=log)
        children.append((name, process))

    def request_stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    PID.write_text(str(os.getpid()))
    try:
        resolution = os.environ.get("BLENDER_DESKTOP_RESOLUTION", "1280x800")
        if not re.fullmatch(r"[0-9]{3,4}x[0-9]{3,4}", resolution):
            raise ValueError("Resolution must look like 1280x800")
        spawn(
            "display",
            ["Xvfb", DISPLAY, "-screen", "0", resolution + "x24", "-nolisten", "tcp", "-ac"],
        )
        for _ in range(100):
            if subprocess.run(
                ["xdpyinfo"],
                env=ENV,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Virtual display failed; see {STATE}/display.log")

        spawn("window-manager", ["openbox"])
        spawn(
            "vnc",
            [
                "x11vnc",
                "-display",
                DISPLAY,
                "-localhost",
                "-rfbport",
                "5900",
                "-forever",
                "-shared",
                "-nopw",
                "-noxdamage",
                "-quiet",
            ],
        )
        spawn(
            "web",
            [
                "websockify",
                "--web=/usr/share/novnc",
                f"127.0.0.1:{PORT}",
                "127.0.0.1:5900",
            ],
        )
        blender = os.environ.get("BLENDER_DESKTOP_BIN") or shutil.which("blender")
        if not blender:
            raise RuntimeError("Blender not found; run setup/blender-codespaces.py install")
        profile_env = dict(ENV, BLENDER_CODESPACES_ROOT=str(ROOT))
        log = (STATE / "blender.log").open("a")
        logs.append(log)
        children.append(
            (
                "blender",
                subprocess.Popen(
                    [blender, "--threads", "2", "--python-expr", BLENDER_PROFILE],
                    env=profile_env,
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                ),
            )
        )

        while True:
            for name, process in children:
                if name in ("display", "vnc", "web") and process.poll() is not None:
                    raise RuntimeError(f"{name} exited; see {STATE}/{name}.log")
            time.sleep(1)
    finally:
        for _, process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for _, process in reversed(children):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        PID.unlink(missing_ok=True)
        for log in logs:
            log.close()


def start():
    if not supervisor_pid():
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "supervisor.log").open("a") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=ROOT,
            )
        for _ in range(150):
            if process.poll() is not None:
                sys.exit(f"Start failed. See {STATE}/supervisor.log")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/vnc.html", timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            sys.exit(f"Desktop startup timed out. See {STATE}")
    print("Open Blender: " + desktop_url())


def stop():
    pid = supervisor_pid()
    if not pid:
        print("Blender desktop stopped.")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(200):
        if not supervisor_pid():
            print("Blender desktop stopped.")
            return
        time.sleep(0.1)
    sys.exit(f"Desktop did not stop. See {STATE}")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if "--serve" in sys.argv:
        serve()
    elif action == "install":
        install()
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "restart":
        stop()
        start()
    elif action == "status":
        print(f"Desktop supervisor: {supervisor_pid() or 'stopped'}")
        subprocess.run(["pgrep", "-a", "-u", str(os.getuid()), "-x", "blender"])
        print("Open Blender: " + desktop_url())
        print("Logs: " + str(STATE))
    else:
        sys.exit(
            "Usage: python3 setup/blender-codespaces.py "
            "[install|start|stop|restart|status]"
        )


if __name__ == "__main__":
    main()
