FROM python:3.13-slim
WORKDIR /app
COPY ./src/orchestrator_agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY ./src /app/src
ENV PORT=8088
EXPOSE 8088
CMD ["python", "-m", "src.orchestrator_agent.agent"]
