FROM python:3.12-alpine

# Unbuffered, so `docker run` without a TTY still shows the scan as it happens rather than dumping
# everything at exit. No bytecode files either, which keeps the image clean and lets the container
# run with a read-only root filesystem.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY verifyarr.py .

# Not root. 1000:1000 suits a typical Linux or NAS host; override with `--user $(id -u):$(id -g)` if
# yours differs (Unraid is commonly 99:100). Otherwise the state file this writes into your appdata
# ends up owned by root and bites you the next time you run the tool as yourself.
USER 1000:1000

ENTRYPOINT ["python3", "verifyarr.py"]

# scan, never replace, by default: report mode only ever adds a tag and writes its state file.
CMD ["scan"]