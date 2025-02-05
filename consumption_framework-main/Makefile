# Makefile
IMAGE_NAME = my-consumption
IMAGE_LAMBDA_NAME ?= my-consumption
VERSION ?= v1.0.0
BRANCH = main
LATEST_COMMIT = $(shell git rev-parse $(BRANCH) 2>/dev/null || echo v1) 

AWS_ROLE_NAME = my_consumption_framework_role
AWS_LAYER_NAME = my_consumption_framework_dependencies
AWS_FUNCTION_NAME = my_consumption-framework
AWS_FUNCTION_IMAGE_NAME = my-consumption-framework-image
AWS_REGION = us-east-2
AWS_ECR_REPOSITORY = 461485115270.dkr.ecr.us-east-2.amazonaws.com

LAMBDA_TARGET ?= lambda_package

.PHONY: build-and-release
build-and-release: build release

.PHONY: build
build:
	docker build -t $(IMAGE_NAME):$(LATEST_COMMIT) .

run-image:
	docker run -it --rm -v $(PWD)/config.yml:/config.yml $(IMAGE_NAME):$(LATEST_COMMIT) --help

release:
	zip -r $(VERSION).zip README.md Dockerfile Makefile config.yml.sample consumption entrypoint.sh kibana_exports main.py requirements*.txt -x "**/__pycache__/*"
	docker push $(IMAGE_NAME):$(VERSION)
	@echo "Pushed Docker image: $(IMAGE_NAME):$(VERSION)"

.PHONY: lambda-layers
lambda-layers:
	echo "Creating dependencies layer"
	mkdir -p $(LAMBDA_TARGET)/python
	pip3 install -r requirements_lambda.txt -t $(LAMBDA_TARGET)/python --platform manylinux2014_aarch64 --implementation cp --only-binary=:all: --upgrade
	cd $(LAMBDA_TARGET) && zip -r ../$(LAMBDA_TARGET)_dependencies.zip python -x "*/__pycache__*" > /dev/null
	echo "Creating main layer"
	zip -r $(LAMBDA_TARGET)_main.zip consumption/ -x "*/__pycache__*" > /dev/null
	zip $(LAMBDA_TARGET)_main.zip lambda_function.py

.PHONY: lambda-release
lambda-release:
	ROLE_ARN=$(shell aws iam create-role \
		--role-name $(AWS_ROLE_NAME) \
		--assume-role-policy-document '{"Version": "2012-10-17","Statement": [{ "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}' \
		--query Role.Arn \
		--output text \
		--no-paginate \
		--no-cli-pager) && \
	DEPENDENCIES_LAYER_ARN=$(shell aws lambda publish-layer-version \
		--layer-name $(AWS_LAYER_NAME) \
		--zip-file fileb://$(LAMBDA_TARGET)_dependencies.zip \
		--compatible-runtimes python3.11 \
		--compatible-architectures arm64 \
		--query LayerVersionArn \
		--output text \
		--no-paginate \
		--no-cli-pager) && \
	echo "Role ARN: $$ROLE_ARN" && \
	echo "Dependencies Layer ARN: $$DEPENDENCIES_LAYER_ARN" && \
	aws lambda create-function \
		--role $$ROLE_ARN \
		--function-name $(AWS_FUNCTION_NAME) \
		--runtime python3.11 \
		--handler lambda_function.handler \
		--zip-file fileb://$(LAMBDA_TARGET)_main.zip \
		--timeout 300 \
		--layers $$DEPENDENCIES_LAYER_ARN \
		--architectures arm64 \
		--no-cli-pager
	aws iam attach-role-policy --role-name $(AWS_ROLE_NAME) --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

.PHONY: clean
clean:
	docker rmi -f $(IMAGE_NAME):$(VERSION) || true
	rm -rf lambda_package*

lambda-delete-function-image:
	@aws lambda delete-function \
		--function-name $(AWS_FUNCTION_IMAGE_NAME) \
		--region $(AWS_REGION) && \
	echo "Lambda function $(AWS_FUNCTION_IMAGE_NAME) deleted successfully."

lambda-create-role-attach-policy:
	@echo "Creating role $(AWS_ROLE_NAME)..."
	@ROLE_ARN=$$(aws iam create-role \
		--role-name $(AWS_ROLE_NAME) \
		--assume-role-policy-document file://aws-role-lambda.json \
		--query 'Role.Arn' \
		--output text \
		--no-paginate \
		--no-cli-pager) && \
	echo "Role ARN: $$ROLE_ARN"
	@echo "Attaching AWSLambdaBasicExecutionRole to $(AWS_ROLE_NAME)..."
	@aws iam attach-role-policy \
		--role-name $(AWS_ROLE_NAME) \
		--policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

lambda-delete-role:
  # Detach all inline policies (if any) attached to the role
	$(shell aws iam list-role-policies --role-name $(AWS_ROLE_NAME) --query 'PolicyNames[]' --output text | xargs -I {} aws iam delete-role-policy --role-name $(AWS_ROLE_NAME) --policy-name {})
	# Detach all managed policies attached to the role (if any)
	$(shell aws iam list-attached-role-policies --role-name $(AWS_ROLE_NAME) --query 'AttachedPolicies[].PolicyArn' --output text | xargs -I {} aws iam detach-role-policy --role-name $(AWS_ROLE_NAME) --policy-arn {})
	# Finally, delete the role itself
	$(shell aws iam delete-role --role-name $(AWS_ROLE_NAME))
	@echo "Role $(AWS_ROLE_NAME) deleted successfully."

lambda-create-function-image:
	@ROLE_ARN=$$(aws iam get-role \
		--role-name $(AWS_ROLE_NAME) \
		--query 'Role.Arn' \
		--output text) && \
	aws lambda create-function \
		--function-name $(AWS_FUNCTION_IMAGE_NAME) \
    --package-type Image \
    --code ImageUri=$(AWS_ECR_REPOSITORY)/$(IMAGE_LAMBDA_NAME):$(LATEST_COMMIT) \
		--role $$ROLE_ARN \
		--timeout 10 --memory-size 128 --region $(AWS_REGION)

lambda-build-image:
	docker build -f Dockerfile.lambda --platform linux/amd64 -t $(IMAGE_LAMBDA_NAME):$(LATEST_COMMIT) .

lambda-push-image:
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ECR_REPOSITORY)
	docker tag $(IMAGE_LAMBDA_NAME):$(LATEST_COMMIT) $(AWS_ECR_REPOSITORY)/$(IMAGE_LAMBDA_NAME):$(LATEST_COMMIT)
	docker push $(AWS_ECR_REPOSITORY)/$(IMAGE_LAMBDA_NAME):$(LATEST_COMMIT)

