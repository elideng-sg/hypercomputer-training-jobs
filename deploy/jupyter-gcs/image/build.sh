#!/usr/bin/env bash
#
# Build and push both notebook images.
#
#   deploy/jupyter-gcs/image/build.sh            # both, push
#   TARGET=cpu deploy/jupyter-gcs/image/build.sh # just the slim one
#   PUSH=0 deploy/jupyter-gcs/image/build.sh     # build locally only
#
# Uses Cloud Build by default -- the CUDA target is ~10GB and building it on a
# laptop then pushing over a home connection is a bad afternoon. Set
# BUILDER=docker to build locally.
set -euo pipefail

PROJECT="${PROJECT:-hdlab-elideng}"
# asia-southeast1, co-located with the cluster. The existing `lab-images` repo is
# in asia-east1; a cross-region pull of a 10GB image on every new node is slow
# and billed as inter-region egress. Repo names only need to be unique per
# location, so this is also called lab-images.
REGION="${REGION:-asia-southeast1}"
REPO="${REPO:-lab-images}"
TAG="${TAG:-v1}"
TARGET="${TARGET:-both}"
PUSH="${PUSH:-1}"
BUILDER="${BUILDER:-cloudbuild}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ "$BUILDER" == "cloudbuild" && "$PUSH" != "1" ]]; then
  echo "PUSH=0 is not possible with Cloud Build -- it pushes as part of the build." >&2
  echo "Use BUILDER=docker PUSH=0 for a local-only build." >&2
  exit 2
fi

echo "== ensuring Artifact Registry repo ${REPO} in ${REGION}"
if gcloud artifacts repositories describe "$REPO" \
     --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  echo "repo exists"
else
  gcloud artifacts repositories create "$REPO" \
    --project="$PROJECT" --location="$REGION" \
    --repository-format=docker \
    --description="Notebook images for the TCPXO JupyterHub"
fi

build_one() {
  local name="$1" base="$2"
  local image="${REGISTRY}/${name}:${TAG}"
  echo
  echo "== ${image}"
  echo "   base ${base}"

  if [[ "$BUILDER" == "cloudbuild" ]]; then
    # --tag and --config are mutually exclusive, so the image name travels as a
    # substitution instead.
    gcloud builds submit "$HERE" \
      --project="$PROJECT" --region="$REGION" \
      --config="${HERE}/cloudbuild.yaml" \
      --substitutions="_BASE_IMAGE=${base},_IMAGE=${image}"
  else
    docker build --build-arg "BASE_IMAGE=${base}" -t "$image" "$HERE"
    if [[ "$PUSH" == "1" ]]; then
      gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
      docker push "$image"
    fi
  fi
}

if [[ "$TARGET" == "cpu" || "$TARGET" == "both" ]]; then
  build_one notebook-gcs "quay.io/jupyter/minimal-notebook:latest"
fi
if [[ "$TARGET" == "gpu" || "$TARGET" == "both" ]]; then
  # Same base the GPU profiles ran before, so CUDA/torch behaviour is unchanged.
  build_one pytorch-notebook-gcs "quay.io/jupyter/pytorch-notebook:cuda12-latest"
fi

echo
echo "Images built. They are referenced by tag ${TAG} in"
echo "deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml -- bump TAG there too"
echo "when you rebuild, or running pods will keep the cached ${TAG}."
