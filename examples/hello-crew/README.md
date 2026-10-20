# Hello Crew

This is a tiny practice project for the agent-crew-chief tutorials. It has no runtime dependencies. The tutorials add and test a small function with ACC.

## Copy it for a tutorial

Copy this folder to a new location, then enter the copy:

```bash
cp -R examples/hello-crew ~/projects/hello-crew
cd ~/projects/hello-crew
git init
git add .
git commit -m "Start hello-crew practice project"
```

Run the tests with `pytest`. The package uses a `src/` layout, so the tutorial commands install ACC into this Git repository before running jobs.
