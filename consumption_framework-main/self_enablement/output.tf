output "host-public" {
  value = google_compute_instance.host.network_interface.0.access_config.0.nat_ip
}

