PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
ENABLED_POOLS ?= l4,a100,h100-high,h100-mega

.PHONY: bootstrap render compose plan up configure down verify-cost
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
	$(MAKE) configure

configure:
	cd terraform && terraform init -backend-config="bucket=$(PROJECT)-lab-tfstate" -backend-config="prefix=$(REGION)-post-cluster" && terraform apply -auto-approve -var-file=../env/$(REGION).tfvars

down:
	@if [ "$(DESTROY)" = "1" ]; then \
	  cd build/hpc-lab/primary && terraform destroy -auto-approve -var-file=../../../env/$(REGION).tfvars; \
	else \
	  kubectl delete provisioningrequests --all -A --ignore-not-found; \
	  for p in $$(echo $(ENABLED_POOLS) | tr ',' ' '); do \
	    gcloud container clusters resize hpc-lab --node-pool $$p --region $(REGION) --num-nodes 0 --quiet || true; done; \
	fi

verify-cost:
	python3 scripts/assert_zero_gpu.py $(PROJECT)

smoke:
	@if [ -z "$(GPU)" ]; then echo "Error: GPU type required. Usage: make smoke GPU=<type>"; exit 1; fi
	kubectl create -f manifests/smoke/nccl-smoke.yaml --dry-run=client -o yaml | sed 's/__GPU__/$(GPU)/g' | kubectl apply -n team-a -f -
	kubectl wait --for=condition=complete job/nccl-smoke-$(GPU) -n team-a --timeout=45m || true
	kubectl logs job/nccl-smoke-$(GPU) -n team-a | tail -30 || true
	kubectl delete job nccl-smoke-$(GPU) -n team-a --ignore-not-found
