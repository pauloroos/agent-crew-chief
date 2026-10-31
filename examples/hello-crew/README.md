# Hello Crew

The tutorial example for agent-crew-chief (ACC). Follow `docs/tutorial.md` in the ACC repository.

Copy this folder out of the ACC checkout and make it its own Git repository before use:

```bash
cp -R examples/hello-crew ~/projects/hello-crew && cd ~/projects/hello-crew
git init && git add . && git commit -m "Start hello-crew"
python -m pytest -q
```
