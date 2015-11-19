
.. _configuration:

######################
Configuring a Git Repo
######################

Since version 3, *Scap* is now able to deploy any git-based repository from tin
to any number of hosts. This deployment can happen in serial or in parallel. All
that is necessary, aside from the configuration outlined here, is that the target
hosts are accessible via SSH by the ``deploy_user`` from the ``deploy_host``
(the machine from which you run Scap).

Scap configuration is loaded from several files via :func:`scap.config.load`
function in the :mod:`scap.config` module.

.. autofunction:: scap.config.load


Simple initial setup
~~~~~~~~~~~~~~~~~~~~~~

For a new repo setup, the main file that needs to be created is in the root of
the repository at ``scap/scap.cfg``. This new file should be made in
``ConfigParser`` format.

.. warning::
   These values **must be** set in ``scap/scap.cfg``:
    - ``git_repo``
    - ``git_repo_user``
    - ``dsh_targets``.

An example of a sensible default config file is seen here::

    [global]
    ssh_user: service-deploy
    git_repo: mockbase/deploy
    git_repo_user: service-deploy
    dsh_targets: mockbase
    git_submodules: True
    service_name: mockbase
    service_port: 1134
    batch_size: 80
    promote_batch_size: 1
    config_deploy: True

    [wmflabs]
    git_server: deployment-bastion.deployment-prep.eqiad.wmflabs

    [wmnet]
    git_server: tin.eqiad.wmnet

.. _available-configuration:

Available configuration variables
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
+-----------------------------+----------------------------+------------------------------------+
| Value                       | Default                    | Explanation                        |
+=============================+============================+====================================+
|  ``git_server``             | ``tin.eqiad.wmnet``        | (*String*) Server from             |
|                             |                            | which code is fetched              |
+-----------------------------+----------------------------+------------------------------------+
|  ``ssh_user``               | your username              | (*String*) User as whom            |
|                             |                            | to ssh to target hosts             |
+-----------------------------+----------------------------+------------------------------------+
|  ``git_repo``               | **NONE**                   | (*String*) The repo to             |
|                             |                            | deploy under                       |
|                             |                            | ``git_deploy_dir``                 |
+-----------------------------+----------------------------+------------------------------------+
| ``git_repo_user``           | ``mwdeploy``               | (*String*) User as whom            |
|                             |                            | to make repo changes               |
|                             |                            | like ``git update-server-info``    |
|                             |                            | ``git checkout`` on remote         |
|                             |                            | targets, etc.                      |
+-----------------------------+----------------------------+------------------------------------+
| ``git_deploy_dir``          | ``/srv/deployment``        | (*String*) Directory on            |
|                             |                            | ``git_server`` in which            |
|                             |                            | your repo is found                 |
+-----------------------------+----------------------------+------------------------------------+
| ``dsh_targets``             | ``mediawiki-installation`` | (*String*) Path to list            |
|                             |                            | of deploy targets. If              |
|                             |                            | the path is not absolute,          |
|                             |                            | Scap looks for a file in           |
|                             |                            | ``/etc/dsh/groups``. For           |
|                             |                            | an absolute path, it just          |
|                             |                            | uses the full path to              |
|                             |                            | the file.                          |
+-----------------------------+----------------------------+------------------------------------+
| ``server_groups``           | **NONE**                   | (*String*) (*Optional*)            |
|                             |                            | If this option is defined,         |
|                             |                            | Scap will look deploy to           |
|                             |                            | these *groups* of servers in order.|
|                             |                            |                                    |
|                             |                            | For Example:                       |
|                             |                            | ``server_groups: 'can, default'``  |
|                             |                            | will cause Scap to look in         |
|                             |                            | the ``scap.cfg`` file for          |
|                             |                            | both a ``can_dsh_targets``         |
|                             |                            | file and a ``dsh_targets``         |
|                             |                            | file (the `default`). A full       |
|                             |                            | deploy will run for hosts          |
|                             |                            | defined in ``can_dsh_targets``     |
|                             |                            | then a full deploy                 |
|                             |                            | will run for hosts defined         |
|                             |                            | in ``dsh_targets``                 |
|                             |                            | (any hosts defined in both         |
|                             |                            | will be deployed with the          |
|                             |                            | first group--``can``               |
|                             |                            | in this example)                   |
+-----------------------------+----------------------------+------------------------------------+
| ``git_submodules``          | False                      | (*Boolean*) (*Optional*)           |
|                             |                            | Whether submodules need            |
|                             |                            | to be fetched and                  |
|                             |                            | checked-out as part of             |
|                             |                            | the deploy on targets.             |
+-----------------------------+----------------------------+------------------------------------+
| ``service_name``            | **NONE**                   | (*String*) (*Optional*)            |
|                             |                            | If a service name is               |
|                             |                            | defined, the service               |
|                             |                            | will be restarted on               |
|                             |                            | each target as part                |
|                             |                            | of the ``promote``                 |
|                             |                            | stage.                             |
+-----------------------------+----------------------------+------------------------------------+
| ``service_port``            | **NONE**                   | (*Int*) (*Optional*)               |
|                             |                            | If a port is defined,              |
|                             |                            | Scap will verify that              |
|                             |                            | the port is accepting TCP          |
|                             |                            | connections after the              |
|                             |                            | ``promote`` deploy stage.          |
|                             |                            | (Timeout defined by                |
|                             |                            | ``service_timeout``)               |
+-----------------------------+----------------------------+------------------------------------+
| ``service_timeout``         | 120                        | (*Int*) (*Optional*) The           |
|                             |                            | amount of time to wait             |
|                             |                            | when checking a                    |
|                             |                            | ``service_port``                   |
|                             |                            | for accepting TCP                  |
|                             |                            | connections.                       |
+-----------------------------+----------------------------+------------------------------------+
| ``batch_size``              | 80                         | (*String*) Parallelism             |
| ``[stage]_batch_size``      |                            | of a stage of deployment.          |
|                             |                            | Number of hosts to                 |
|                             |                            | execute a particular               |
|                             |                            | deployment stage on                |
|                             |                            | simultaniously. This               |
|                             |                            | is configurable by                 |
|                             |                            | stage by creating                  |
|                             |                            | a config variable:                 |
|                             |                            | ``[stage]_batch_size``             |
+-----------------------------+----------------------------+------------------------------------+
| ``config_deploy``           | False                      | (*Boolean*) (*Optional*)           |
|                             |                            | if ``True``, the                   |
|                             |                            | ``./scap/config-files.yaml``       |
|                             |                            | file will be parsed, any           |
|                             |                            | templates defined inside           |
|                             |                            | will be evaluated with jinja2      |
|                             |                            | and deployed.                      |
+-----------------------------+----------------------------+------------------------------------+
| ``git_upstream_submodules`` | False                      | (*Boolean*) If ``True``,           |
|                             |                            | submodules will **NOT** be         |
|                             |                            | fetched from                       |
|                             |                            | ``git_deploy_server``,             |
|                             |                            | but from the git server            |
|                             |                            | defined in ``.gitmodules``         |
+-----------------------------+----------------------------+------------------------------------+
| ``nrpe_dir``                | ``/etc/nagios/nrpe.d``     | (*String*) Directory in            |
|                             |                            | which nrpe checks are              |
|                             |                            | stored                             |
+-----------------------------+----------------------------+------------------------------------+
| ``perform_checks``          | True                       | (*Boolean*) If ``True``,           |
|                             |                            | checks defined in                  |
|                             |                            | ``./scap/checks.yaml``             |
|                             |                            | will be performed after            |
|                             |                            | each-stage of checkout.            |
+-----------------------------+----------------------------+------------------------------------+
