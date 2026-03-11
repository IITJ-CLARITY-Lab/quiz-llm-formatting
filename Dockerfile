FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements_streamlit_app.txt /app/requirements_streamlit_app.txt
RUN pip install --no-cache-dir -r /app/requirements_streamlit_app.txt

COPY . /app

EXPOSE 11001

CMD ["sh", "-c", "python /app/bootstrap.py && streamlit run /app/app.py --server.address=0.0.0.0 --server.port=11001 --server.headless=true"]
