PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
ENABLED_POOLS ?= l4,a100,h100-high,h100-mega

.PHONY: bootstrap render plan up
bootstrap:
	PROJECT=$(PROJECT) REGION=$(REGION) bash scripts/bootstrap.sh
	python3 scripts/preflight.py --project $(PROJECT) --region $(REGION) --pools $(ENABLED_POOLS)

render:
	gcluster create blueprints/base-cluster.yaml --vars project_id=$(PROJECT),region=$(REGION) -o build/

plan: render
	cd build/hpc-lab/primary && terraform init -backend-config="bucket=$(PROJECT)-lab-tfstate" -backend-config="prefix=$(REGION)" && terraform validate && terraform plan -var-file=../../../env/$(REGION).tfvars

up: plan
	cd build/hpc-lab/primary && terraform apply -auto-approve -var-file=../../../env/$(REGION).tfvars
