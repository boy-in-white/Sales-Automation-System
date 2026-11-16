#!/usr/bin/env python3
"""Temporary HTTPS for Repliq; Cloudflare only reaches the callback listener."""

import argparse
import io
import os
import platform
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import time
import webbrowser
from urllib.request import urlopen

from app import AppError, ROOT, Repliq, make_server, read_json, write_private

PUBLIC_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com(?=[\s/|]|$)")


def cloudflared_binary():
    installed = shutil.which("cloudflared")
    if installed:
        return installed
    binary = ROOT / ".tools" / "cloudflared"
    if binary.is_file():
        return str(binary)
    if platform.system() != "Darwin":
        raise AppError("Спочатку встанови офіційний cloudflared: https://developers.cloudflare.com/tunnel/downloads/")
    arch = "arm64" if platform.machine() == "arm64" else "amd64"
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-%s.tgz" % arch
    print("Завантажую офіційний cloudflared у папку Repliq (лише під час першого запуску)…", flush=True)
    with urlopen(url, timeout=60) as response:
        content = response.read(100 * 1024 * 1024 + 1)
    if len(content) > 100 * 1024 * 1024:
        raise AppError("Завеликий архів cloudflared. Завантаження скасовано.")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        members = [member for member in archive.getmembers()
                   if member.name.removeprefix("./") == "cloudflared" and member.isfile()]
        if len(members) != 1:
            raise AppError("Неочікуваний формат офіційного архіву cloudflared.")
        binary.parent.mkdir(parents=True, exist_ok=True)
        temporary = binary.with_suffix(".download")
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
            with os.fdopen(fd, "wb") as target, archive.extractfile(members[0]) as source:
                shutil.copyfileobj(source, target)
            os.chmod(temporary, 0o700)
            os.replace(temporary, binary)
        finally:
            if temporary.exists():
                temporary.unlink()
    return str(binary)


def main():
    parser = argparse.ArgumentParser(description="Repliq — тимчасова публічна HTTPS-адреса для OLX")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--callback-port", type=int, default=8788)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if (args.port == args.callback_port or
            not all(1024 <= port <= 65535 for port in (args.port, args.callback_port))):
        parser.error("Вкажи два різні порти від 1024 до 65535.")
    servers = []
    tunnel = None
    threads = []
    try:
        binary = cloudflared_binary()
        app = Repliq(args.port)
        servers.append(make_server(app))
        servers.append(make_server(app, args.callback_port, public_only=True))
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        print("Створюю тимчасову HTTPS-адресу через Cloudflare…", flush=True)
        tunnel = subprocess.Popen([
            binary, "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", "http://127.0.0.1:%s" % args.callback_port,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output = queue.Queue()

        def receive():
            try:
                for line in tunnel.stdout:
                    output.put(line)
            except (OSError, ValueError):
                pass
            finally:
                output.put(None)

        threading.Thread(target=receive, daemon=True).start()
        deadline = time.monotonic() + 90
        address = None
        while True:
            if not address and time.monotonic() > deadline:
                raise AppError("Cloudflare не надав адресу за 90 секунд. Перевір інтернет і повтори запуск.")
            try:
                line = output.get(timeout=0.25)
            except queue.Empty:
                if tunnel.poll() is not None:
                    raise AppError("З’єднання Cloudflare завершилося. Повтори публічний запуск Repliq.")
                continue
            if line is None:
                raise AppError("З’єднання Cloudflare завершилося. Повтори публічний запуск Repliq.")
            match = PUBLIC_URL.search(line)
            if match and not address:
                address = match.group(0) + "/oauth/callback"
                app.save_settings(dict(app.settings, callback_uri=address))
                if read_json(app.tokens_path).get("access_token"):
                    app.notice = "Підключення OLX збережено. Нову HTTPS-адресу потрібно вказати в OLX лише перед повторною авторизацією."
                else:
                    app.notice = "Публічну HTTPS-адресу створено. Скопіюй її в заявку OLX і залиш це вікно відкритим."
                write_private(app.data_dir / "public-address.json", {"callback_uri": address, "pid": os.getpid()})
                print("Repliq: " + app.local_url, flush=True)
                print("URI для форми OLX: " + address, flush=True)
                print("Адреса тимчасова. Після повторного запуску зміниться. Не закривай це вікно під час авторизації.", flush=True)
                print("Щоб зупинити, натисни Ctrl+C.", flush=True)
                if args.open:
                    webbrowser.open(app.local_url)
            # Cloudflare logs are deliberately not forwarded: URLs may contain OAuth codes.
    except KeyboardInterrupt:
        print("Repliq зупинено. Тимчасова HTTPS-адреса більше не працює.", flush=True)
    except (AppError, OSError, ValueError, tarfile.TarError) as error:
        if isinstance(error, AppError):
            print(str(error), flush=True)
        else:
            print("Не вдалося запустити Repliq. Закрий попередній запуск, перевір інтернет і повтори.", flush=True)
        return 1
    finally:
        if tunnel is not None:
            if tunnel.poll() is None:
                tunnel.terminate()
                try:
                    tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tunnel.kill()
                    tunnel.wait(timeout=5)
            if tunnel.stdout:
                tunnel.stdout.close()
        for server in servers:
            if threads:
                server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
