PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
ENABLED_POOLS ?= l4,a100,h100-high,h100-mega

.PHONY: bootstrap render compose plan up
bootstrap:
	PROJECT=$(PROJECT) REGION=$(REGION) bash scripts/bootstrap.sh
	python3 scripts/preflight.py --project $(PROJECT) --region $(REGION) --pools $(ENABLED_POOLS)

render: compose
	gcluster create build/blueprint.yaml --vars project_id=$(PROJECT),region=$(REGION) -o build/

compose:
	@mkdir -p build
	@cp blueprints/base-cluster.yaml build/blueprint.yaml
	@IFS=','; for pool in $(ENABLED_POOLS); do \
		if [ -f "blueprints/pools/$$pool.yaml" ]; then \
			echo "  # Pool: $$pool" >> build/blueprint.yaml; \
			cat "blueprints/pools/$$pool.yaml" >> build/blueprint.yaml; \
		else \
			echo "Warning: blueprints/pools/$$pool.yaml not found" >&2; \
		fi \
	done
	@echo "Composed blueprint with pools: $(ENABLED_POOLS)"

plan: render
	cd build/hpc-lab/primary && terraform init -backend-config="bucket=$(PROJECT)-lab-tfstate" -backend-config="prefix=$(REGION)" && terraform validate && terraform plan -var-file=../../../env/$(REGION).tfvars

up: plan
	cd build/hpc-lab/primary && terraform apply -auto-approve -var-file=../../../env/$(REGION).tfvars
