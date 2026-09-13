import uvicorn

uvicorn.run("mini_agent.api:app", host="127.0.0.1", port=8000, reload=False)
