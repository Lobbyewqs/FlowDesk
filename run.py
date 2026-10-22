import argparse
import threading
import webbrowser

import uvicorn


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动 FlowDesk 本地演示平台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动时打开浏览器")
    args = parser.parse_args()
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    uvicorn.run("flowdesk.main:app", host="127.0.0.1", port=args.port, workers=1)

