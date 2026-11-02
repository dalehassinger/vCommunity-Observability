"""dbh-topology collector: hosts, clusters, datastores and mounts (no VMs needed)."""
from fabric import main

if __name__ == "__main__":
    main(with_vms=False)
