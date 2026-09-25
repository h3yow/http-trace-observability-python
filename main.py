"""开发入口：以随机端口启动可观测 HTTP 服务。"""
import uvicorn


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=0)
