FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir setuptools wheel

# Copy source
COPY *.py ./
COPY skills/ skills/
COPY static/ static/

# Install package
RUN pip install --no-cache-dir -e .

# Create runtime directories
RUN mkdir -p logs memory skills uploads

EXPOSE 7860

CMD ["qwe-qwe", "--web"]
