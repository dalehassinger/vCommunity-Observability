"""dbh-impact collector: the fabric (hosts, clusters, datastores, mounts, VMs) plus a
finished impact answer for every host and every datastore."""
from fabric import main
from impact import impact_rows

if __name__ == "__main__":
    main(with_vms=True, extend=lambda tables: {"impact": impact_rows(tables)})
