# LiveSupport Web Chat

A minimal web chat interface that lets a browser user exchange messages with an AWS Bedrock AgentCore Runtime agent (`livesupport-agent`). A Python/FastAPI backend (`app.py`) sits between the browser and AgentCore Runtime: it receives chat requests over HTTP, invokes the agent runtime via boto3 using IAM (SigV4) credentials from an EC2 instance role, and streams the agent's response back to the browser over Server-Sent Events (SSE). The browser never calls AgentCore directly.

## Project Structure

```
livesupport-web/
├── app.py              # FastAPI app: routes, SSE wiring, rate limiting, error mapping
├── agent_client.py     # boto3 bedrock-agentcore client wrapper + SSE line parser
├── static/
│   ├── index.html      # Chat page markup
│   └── chat.js         # Session id management, fetch + SSE consumption, DOM rendering
├── requirements.txt    # fastapi, uvicorn, boto3, python-dotenv, pydantic
├── .env.example         # Template for required environment variables
└── deploy/
    ├── livesupport-web.service    # systemd unit template
    └── nginx-livesupport.conf     # nginx reverse proxy config template
```

## Configuration

Copy `.env.example` to `.env` and fill in the required values (`AGENT_RUNTIME_ARN`, `AWS_REGION`, `RATE_LIMIT_MAX_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`). The backend authenticates to AWS using the default boto3 credential chain, which resolves to the EC2 instance role when deployed.

## Running locally

```bash
pip install -r requirements.txt
# a：active venv
.\.venv\Scripts\Activate.ps1
uvicorn app:app --reload --port 8008
uvicorn app:app --reload --host 0.0.0.0 --port 8008

# b：not active
.\.venv\Scripts\uvicorn.exe app:app --reload --port 8008

# c：System python enviroment
uvicorn app:app --reload --port 8008
```

## Deployment

The service is intended to run on an EC2 instance, managed by systemd, and fronted by nginx as a TLS-terminating reverse proxy. Running the systemd commands alone is **not** sufficient — the steps below must be completed in order, and the service will not be reachable from a browser until nginx, TLS, and the security group are also configured.

### Full deployment order

#### 1. Prepare the host: user, code, virtualenv, environment file

```bash
# Create the non-root service user the app runs as
sudo useradd --system --no-create-home --shell /usr/sbin/nologin livesupport

# Place the application code at the path referenced by the systemd
# unit's WorkingDirectory
sudo mkdir -p /opt/livesupport-web
# copy/clone the contents of livesupport-web/ into /opt/livesupport-web

cd /opt/livesupport-web
```

**在 Ubuntu 服务器上执行（先删掉打包上传时带过来的 Windows 版 `.venv`）：**

If the uploaded archive includes a `.venv` created on Windows, it will not work on Linux — the internal layout (`Scripts/` + `.ps1`/`.exe` files) and paths baked into `pyvenv.cfg` are Windows-specific. Delete it and create a fresh Linux virtualenv:

```bash
# 删除从 Windows 打包带过来的、不兼容的 .venv
rm -rf .venv

# 确认已装 python3 的 venv 模块（Ubuntu 有时需要单独装）
sudo apt update
sudo apt install -y python3-venv

# 新建虚拟环境
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Create the environment file from the template and fill in real values
# (AGENT_RUNTIME_ARN, AWS_REGION, RATE_LIMIT_MAX_REQUESTS,
# RATE_LIMIT_WINDOW_SECONDS)
cp .env.example .env
nano .env   # or your editor of choice

# Restrict ownership/permissions: the app runs as `livesupport`, and
# .env must not be world-readable
sudo chown -R livesupport:livesupport /opt/livesupport-web
sudo chmod 600 /opt/livesupport-web/.env
```

#### 2. Install and start the systemd service

Use the unit file template at [`deploy/livesupport-web.service`](deploy/livesupport-web.service) to run the FastAPI app via `uvicorn` as a managed service. It documents the `ExecStart` command, the working directory, the `.env` environment file reference, the restart policy, and the non-root user the service runs as.

```bash
sudo cp deploy/livesupport-web.service /etc/systemd/system/livesupport-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now livesupport-web

# Confirm it started successfully before moving on
sudo systemctl status livesupport-web
```

At this point uvicorn is running, but only bound to `127.0.0.1` (localhost) — it is **not** reachable from outside the host yet.

##### Alternative: running with `nohup` instead of systemd

If you don't want to install a systemd service (e.g. quick testing on a Linux box), you can run uvicorn detached from the terminal with `nohup` instead:

```bash
cd /opt/livesupport-web   # or your project directory
source venv/bin/activate  # if using a virtualenv

nohup uvicorn app:app --host 0.0.0.0 --port 8008 > uvicorn.log 2>&1 &
```

- `nohup ... &` runs the command in the background and ignores `SIGHUP`, so the process keeps running after the terminal/SSH session disconnects.
- `> uvicorn.log 2>&1` redirects stdout and stderr to `uvicorn.log`; without it, the logs are lost once the terminal closes.
- Do **not** add `--reload` here — that flag is for local development only and adds a file-watcher process plus unnecessary restarts/overhead in a long-running deployment.

Check it's running and view logs:

```bash
tail -f uvicorn.log
ps aux | grep uvicorn
```

Stop it:

```bash
kill <PID>
# or
pkill -f "uvicorn app:app"
```

Note that `nohup` does **not** restart the process automatically if it crashes or the host reboots. For unattended production use, prefer the systemd service above, which has `Restart=on-failure`.

#### 3. Configure the nginx reverse proxy and TLS

Use the nginx location block template at `deploy/nginx-livesupport.conf` to reverse-proxy requests to the local uvicorn process. Because chat responses are streamed to the browser over SSE, the `/api/chat` location sets `proxy_buffering off;` and an elevated `proxy_read_timeout` so nginx forwards streamed chunks immediately instead of buffering them, and does not cut the connection off during long-running agent responses.

```bash
sudo cp deploy/nginx-livesupport.conf /etc/nginx/snippets/nginx-livesupport.conf
```

This file is a *snippet*, not a standalone server config — include it inside a `server {}` block that terminates TLS (e.g. via certbot/Let's Encrypt) on port 443:

```nginx
server {
    listen 443 ssl;
    server_name chat.example.com;
    ssl_certificate     /etc/letsencrypt/live/chat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;

    include /etc/nginx/snippets/nginx-livesupport.conf;
}
```

```bash
sudo nginx -t
sudo systemctl reload nginx
```

#### 4. Open the EC2 security group

Restrict the EC2 instance's inbound security group rules to only the ports required to serve the chat page over the public internet:

| Port | Protocol | Purpose |
|------|----------|---------|
| 80   | TCP (HTTP)  | Redirects to HTTPS |
| 443  | TCP (HTTPS) | Serves the chat page and `/api/chat` over TLS |

No other inbound ports (including the uvicorn port the app listens on internally, e.g. 8008) should be exposed to the internet; uvicorn should only be reachable from nginx on `localhost`, and administrative access (e.g. SSH) should be restricted separately to trusted IP ranges rather than opened broadly.

#### 5. Attach an IAM role with AgentCore Runtime permissions

Attach an IAM instance role to the EC2 instance granting `bedrock-agentcore:InvokeAgentRuntime`, scoped to the `AGENT_RUNTIME_ARN` configured in `.env`. The default boto3 credential chain picks this up automatically; without it, the backend fails AgentCore Runtime calls with `AccessDeniedException: The security token included in the request is invalid.`

Only once all five steps are complete is the chat page reachable at `https://<your-domain>/`.
