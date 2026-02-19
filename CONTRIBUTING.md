# Contributing

## Dev environment setup

```bash
git clone https://github.com/matheins/codeask.git
cd codeask
python -m venv .venv
source .venv/bin/activate
pip install .
cp .env.example .env
# Fill in your .env values
```

## Running locally

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

## Pull requests

- Keep changes focused — one concern per PR
- Describe what changed and why in the PR description
- Make sure the app starts and basic endpoints work before submitting
