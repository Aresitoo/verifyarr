FROM python:3.12-alpine

WORKDIR /app

COPY verifyarr.py .

ENTRYPOINT ["python3", "verifyarr.py"]
CMD ["scan"]
