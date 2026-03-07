FROM python:3.11-slim

WORKDIR /app

COPY requirements_streamlit_app.txt /app/requirements_streamlit_app.txt
RUN pip install --no-cache-dir -r /app/requirements_streamlit_app.txt

COPY . /app

EXPOSE 12000

CMD ["sh", "-c", "python /app/bootstrap.py && streamlit run /app/app.py --server.address=0.0.0.0 --server.port=12000 --server.headless=true"]
