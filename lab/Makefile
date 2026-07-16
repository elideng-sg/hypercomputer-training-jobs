PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
ENABLED_POOLS ?= l4,a100,h100-high,h100-mega

.PHONY: bootstrap
bootstrap:
	PROJECT=$(PROJECT) REGION=$(REGION) bash scripts/bootstrap.sh
	python3 scripts/preflight.py --project $(PROJECT) --region $(REGION) --pools $(ENABLED_POOLS)
