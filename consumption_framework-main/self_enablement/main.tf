provider "google" {
  credentials = file("/path/to/your/credentials.json")
  project     = "elastic-customer-eng"
  region      = "europe-west1"
  zone        = "europe-west1-b"
}

resource "google_compute_instance" "host" {
  name         = "consumption-framework-enablement-host"
  machine_type = "n2-custom-2-4096"
  labels = {
    division = "field"
    org = "csg"
    team = "customer-architecture"
    project = "YOUR-SLACK-HANDLE"
  }
  boot_disk {
    initialize_params {
      size  = 50
      image = "ubuntu-2310-mantic-amd64-v20231215"
    }
  }

  metadata = {
    ssh-keys = "YOUR-USER:YOUR-SSH-PUBLIC-KEY"
  }

  network_interface {
    network = "default"
    access_config {}
  }
}
