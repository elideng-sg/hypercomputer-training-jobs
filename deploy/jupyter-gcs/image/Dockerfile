# Notebook image with the GCS toolchain the stock images lack.
#
# Verified missing from the images currently in use: gcloud, gsutil,
# google-cloud-storage, gcsfs. Without them ~/gcs is mountable but a user cannot
# do a bulk copy, and `pd.read_csv("gs://...")` fails.
#
# Two targets from one file, via BASE_IMAGE:
#
#   notebook-gcs          slim, for the CPU profile   (~2GB)
#   pytorch-notebook-gcs  CUDA, for the GPU profiles  (~10GB)
#
# Built by ./build.sh. The GPU base is the same image the GPU profiles used
# before this change, so nothing about CUDA or torch moves here.
ARG BASE_IMAGE=quay.io/jupyter/minimal-notebook:latest
FROM ${BASE_IMAGE}

# Pinning: the bases above are moving tags. That is a deliberate trade for a lab
# -- rebuilds pick up security fixes -- but it means an image tag (v1) is not a
# reproducible build. Pin the base by digest before relying on this for a
# published result.

USER root

# gcloud from the official apt repo rather than the tarball, so that apt handles
# upgrades. gsutil comes along with it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl gnupg apt-transport-https ca-certificates; \
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends google-cloud-cli; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

USER ${NB_UID}

# gcsfs gives pandas/xarray native gs:// URLs; google-cloud-storage is the SDK
# for anything the filesystem abstraction cannot express (resumable uploads,
# composing, metadata). huggingface_hub and kaggle are the two dataset sources
# the team actually uses.
#
# pandas and pyarrow are here for the slim base only in effect: the GPU base
# (pytorch-notebook, via scipy-notebook) already ships them, but
# minimal-notebook does NOT. Without them the header's promise above is false on
# the CPU profile -- verified live on 2026-08-27, where a freshly downloaded
# iris.csv in ~/gcs could not be opened because `import pandas` raised
# ModuleNotFoundError. pyarrow covers parquet, which is what most real datasets
# arrive as.
RUN set -eux; \
    mamba install --yes -c conda-forge \
      gcsfs \
      google-cloud-storage \
      "huggingface_hub>=0.24" \
      kaggle \
      pandas \
      pyarrow; \
    mamba clean --all -f -y; \
    fix-permissions "${CONDA_DIR}"; \
    fix-permissions "/home/${NB_USER}"

# Smoke test at build time: a broken image should fail here, not on a user's
# first spawn twenty minutes into a node pull.
RUN set -eux; \
    gcloud --version; \
    python -c "import gcsfs, google.cloud.storage, huggingface_hub, pandas, pyarrow; print('gcs toolchain ok')"

# ~/gcs and ~/shared are created by the gcsfuse CSI mounts at pod start. They are
# deliberately NOT mkdir'd here: an empty directory baked into the image would
# mask a failed mount and look like an empty bucket.
LABEL org.opencontainers.image.title="jupyter notebook with GCS toolchain" \
      org.opencontainers.image.source="https://github.com/elideng-sg/hypercomputer-training-jobs" \
      org.opencontainers.image.description="Jupyter notebook image with gcloud, gcsfs, google-cloud-storage, huggingface_hub and kaggle for per-user GCS workspaces."
