"""
One command to start the whole digital twin (works on Windows, Linux and macOS):

    python run_all.py                 # broker + twin engine + physical simulator
    python run_all.py --no-broker     # you already run Mosquitto / docker-compose
    python run_all.py --no-sim        # physical layer comes from Wokwi / real ESP32
    python run_all.py --speed 120 --reset

Then open http://localhost:8000
"""
import argparse
import os
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-broker", action="store_true")
    ap.add_argument("--no-sim", action="store_true")
    ap.add_argument("--speed", type=float, default=None, help="simulation speed (default 60x)")
    ap.add_argument("--reset", action="store_true", help="delete the database and regenerate history")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    py = sys.executable
    if a.reset:
        import config
        if os.path.exists(config.DB_PATH):
            os.remove(config.DB_PATH)
            print("[run] database removed")

    procs = []
    try:
        if not a.no_broker:
            procs.append(subprocess.Popen([py, "-m", "broker.embedded_broker"], cwd=ROOT, env=env))
            time.sleep(2.0)
        procs.append(subprocess.Popen([py, "-m", "twin.engine"], cwd=ROOT, env=env))
        time.sleep(3.0)
        if not a.no_sim:
            cmd = [py, "-m", "simulator.physical_sim"]
            if a.speed:
                cmd += ["--speed", str(a.speed)]
            procs.append(subprocess.Popen(cmd, cwd=ROOT, env=env))
        if not a.no_browser:
            webbrowser.open("http://localhost:8000")
        print("[run] all processes started - press Ctrl+C to stop")
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("[run] a process exited, stopping the rest")
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
