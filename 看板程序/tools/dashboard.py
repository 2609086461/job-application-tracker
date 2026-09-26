"""
嵌入式校招看板 - 一键启动脚本
双击此文件即可：检查环境 → 启动 FastAPI (uvicorn) → 打开浏览器
"""
import json
import os, subprocess, sys, time, webbrowser
from urllib.request import urlopen
from project_paths import APP_DIR

PROJECT_DIR = APP_DIR
PORT = 8765
URL = f"http://localhost:{PORT}"


def is_running() -> bool:
    try:
        with urlopen(f"{URL}/openapi.json", timeout=1) as response:
            schema = json.load(response)
        return schema.get("info", {}).get("title") == "嵌入式校招看板"
    except (OSError, ValueError, AttributeError):
        return False


def check(name: str) -> bool:
    try:
        r = subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True, cwd=PROJECT_DIR)
        return r.returncode == 0
    except Exception:
        return False


def main():
    os.chdir(PROJECT_DIR)
    if is_running():
        webbrowser.open(URL)
        return
    print("=" * 52)
    print("  嵌入式校招看板  (FastAPI + SPA)")
    print("=" * 52)
    print()

    # 依赖检查
    need = {"fastapi": "fastapi", "uvicorn": "uvicorn[standard]",
            "requests": "requests", "dotenv": "python-dotenv"}
    missing = [pip for mod, pip in need.items() if not check(mod)]
    if missing:
        print(f"[安装] 缺少依赖: {', '.join(missing)}，正在安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", *missing], cwd=PROJECT_DIR, check=True)

    print("[启动] uvicorn app.main:app  ->", URL)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=PROJECT_DIR,
    )

    # 等就绪后开浏览器
    print("[等待] 等待服务就绪...", end="", flush=True)
    time.sleep(3)
    print(" OK")
    print(f"[打开] 浏览器 -> {URL}")
    webbrowser.open(URL)

    print()
    print("=" * 52)
    print(f"  看板已启动: {URL}")
    print(f"  按 Ctrl+C 或关闭此窗口停止服务")
    print("=" * 52)

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("已停止")


if __name__ == "__main__":
    main()
