# GenAI Portfolio

AI projects and experiments built with the Claude API.

## Layout

- `learning/`: short, single-file experiments, one folder per topic. They share the top-level `.venv` and `.env`.
- `projects/`: standalone projects, each with its own dependencies, `.env` and README.

## Learning

| Folder | What it covers |
|---|---|
| [01_api_basics](learning/01_api_basics) | First call to the Messages API |
| [02_streaming](learning/02_streaming) | Streaming responses |
| [03_tool_use](learning/03_tool_use) | Defining and handling a tool (current date/time) |
| [04_evals](learning/04_evals) | Generating a test dataset and running a prompt eval |

## Projects

| Project | What it is |
|---|---|
| [mcp_cli_chat](projects/mcp_cli_chat) | Command-line chat app with an MCP client and server |

## Setup

1. Copy `.env.example` to `.env` and add your `ANTHROPIC_API_KEY`.
2. For the learning scripts, activate the shared venv and run a script, for example:

   ```
   .venv\Scripts\activate
   python learning/02_streaming/streaming.py
   ```

3. For a project, follow the README in its folder.
