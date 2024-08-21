# Self-enablement runbook

This file presents the steps to run the consumption framework on a given cloud subscription.

## Create a VM to run the python script

The terraform files in the same folder as this README can be used to create a VM on GCP. Make sure you modify the necessary variables in `main.tf` before running the below commands.

```
terraform init
terraform apply
VM_IP=$(terraform output --raw host-public)
USER=$(whoami)
```

## Copy the consumption framework code to the VM

```
git clone git@github.com:elastic/consumption_framework.git
scp -r consumption_framework $USER@$VM_IP:~
```

## Install dependencies

The script requires python >=3.11.

```
ssh $USER@$VM_IP
sudo apt-get update && sudo apt-get install -y python3-pip python3-venv
cd consumption_framework
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

## Configure

You will need to create a configuration file, you can use the `config.yml.sample` file as a template.

This file can then be mofified to add the necessary configuration parameters, as described below.

- Go to [cloud.elastic.co/account/keys](https://cloud.elastic.co/account/keys) and create an API key with "Billing admin" permissions.
- From this same page, save the organization ID.
- Put both these information in the config.yml file.
- Recover the `cloud_id` for both `monitoring_source` and `consumption_destination` clusters (per the configuration file).
- Create an API key on the `monitoring_source` and `consumption_destination` clusters, and put them in the config.yml file.

## Run

Re-run the init command, but with the `config.yml` file you just created. This will create the index template and ingest pipeline on the cluster.

```
python3 main.py init --config-file config.yml
```

Then, run the script:

```
python3 main.py get-billing-data --config-file config.yml
python3 main.py consume-monitoring --config-file config.yml
```

You can finally upload the saved object to Kibana and view the dashboards.

