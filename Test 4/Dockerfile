# Test 4 image: one Dockerfile, three application sizes (shop-api:v4-s / v4-m / v4-l).
# The build context is app/ (data.py, requirements.txt) plus the Test 4 files
# (main.py, requirements-ml.txt, make_model.py, this Dockerfile); deploy4.sh and
# local_check4.sh assemble it, so app/ itself stays the code of Tests 1-3.
#
#   docker build --build-arg SIZE=l --build-arg MODEL_MB_L=500 -t shop-api:v4-l <context>
FROM python:3.12-slim

ARG SIZE=s
# model file size in MB for sizes M and L (trees are added until the file reaches it)
ARG MODEL_MB_M=20
ARG MODEL_MB_L=500

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 SIZE=${SIZE}
WORKDIR /app

RUN case "$SIZE" in s|m|l) ;; *) echo "SIZE must be s, m or l"; exit 1 ;; esac

# Dependencies first, so this layer is cached between code changes.
COPY requirements.txt requirements-ml.txt ./
RUN pip install -r requirements.txt \
 && if [ "$SIZE" != "s" ]; then pip install -r requirements-ml.txt; fi

# The model is generated at build time (deterministic, no download) and baked into the image.
COPY make_model.py ./
RUN if [ "$SIZE" = "m" ]; then python make_model.py /app/model/model.joblib --target-mb "$MODEL_MB_M" --samples 20000; fi \
 && if [ "$SIZE" = "l" ]; then python make_model.py /app/model/model.joblib --target-mb "$MODEL_MB_L"; fi

COPY main.py data.py ./

# Cloud Run passes the listening port in the PORT environment variable (default 8080).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
