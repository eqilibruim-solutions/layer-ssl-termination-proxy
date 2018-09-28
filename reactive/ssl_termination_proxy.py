#!/usr/bin/env python3
# Copyright (C) 2017  Ghent University
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
from shutil import rmtree
from subprocess import check_call, run, CalledProcessError

from charms.reactive import (
    set_flag,
    clear_flag,
    when_not,
    when,
    when_any,
    is_flag_set,
)
from charms.reactive.relations import endpoint_from_flag
from charms.reactive.helpers import data_changed

from charmhelpers.core.hookenv import log, config
from charmhelpers.core import templating, unitdata

from charms.layer import status
from charms.layer import lets_encrypt
from charms.layer.nginx_config_helper import (
    NginxConfig,
    NginxConfigError,
    NginxModule,
)


config = config()


########################################################################
# Install
########################################################################

@when('apt.installed.apache2-utils',
      'nginx.available',
      'nginx-config.installed')
@when_not('ssl-termination.installed')
def install_ssl_termination():
    os.makedirs('/etc/nginx/sites-available/ssl-termination', exist_ok=True)
    os.makedirs('/etc/nginx/sites-available/http', exist_ok=True)
    set_flag('ssl-termination.installed')
    status.blocked('Waiting for fqdn subordinates or http relation')


########################################################################
# SSL-termination interface
########################################################################

@when('ssl-termination.installed',
      'endpoint.ssl-termination.update')
@when_not('endpoint.ssl-termination.joined')
def no_ssl_term_relations():
    clean_nginx('/etc/nginx/sites-available/ssl-termination')
    NginxConfig().delete_all_config(NginxModule.STREAM)
    unitdata.kv().set('sslterm.cert-requests', [])
    clear_flag('endpoint.ssl-termination.update')
    set_flag('ssl-termination.report')

@when('ssl-termination.installed',
      'endpoint.ssl-termination.joined')
@when_any('endpoint.ssl-termination.update')
def get_certificate_requests():
    endpoint = endpoint_from_flag('endpoint.ssl-termination.available')
    clear_flag('endpoint.ssl-termination.update')
    cert_requests = endpoint.get_cert_requests()
    if data_changed('sslterm.requests', cert_requests) and cert_requests:
        old_requests = unitdata.kv().get('sslterm.cert-requests', [])
        delete_old_certs(old_requests, cert_requests)
        unitdata.kv().set('sslterm.cert-requests', cert_requests)
        lets_encrypt.set_requested_certificates(cert_requests)
        clean_nginx('/etc/nginx/sites-available/ssl-termination')
        NginxConfig().delete_all_config(NginxModule.STREAM)
        set_flag('ssl-termination.waiting')
    elif not cert_requests:  # If no more cert_requests remove all configs
        unitdata.kv().set('sslterm.cert-requests', [])
        clean_nginx('/etc/nginx/sites-available/ssl-termination')
        NginxConfig().delete_all_config(NginxModule.STREAM)
        set_flag('ssl-termination.report')


@when('ssl-termination.waiting',
      'lets-encrypt.registered',
      'endpoint.ssl-termination.available')
def configure_nginx():
    clear_flag('ssl-termination.waiting')
    endpoint = endpoint_from_flag('endpoint.ssl-termination.available')
    certs = lets_encrypt.live_all()
    cert_requests = endpoint.get_cert_requests()
    # Find the correct fqdn / certificate info
    for request in cert_requests:
        for fqdn in request['fqdn']:
            if fqdn in certs:
                correct_fqdn = fqdn
        juju_unit_name = request['juju_unit'].split('/')[0]
        if 'upstreams' in request and request['upstreams']:
            abs_file_path = '/etc/nginx/sites-available/ssl-termination/' + juju_unit_name
            create_nginx_config(abs_file_path,
                                request['fqdn'],
                                request['upstreams'],
                                juju_unit_name,
                                certs[correct_fqdn],
                                request['credentials'],
                                'htaccess_' + juju_unit_name)
        if 'tcp' in request and request['tcp']:
            if not create_tcp_nginx_config(request['tcp'],
                                           certs[correct_fqdn],
                                           juju_unit_name):
                return
    update_nginx()
    endpoint.send_status(list(certs.keys()))
    set_flag('ssl-termination.report')



########################################################################
# HTTP interface
########################################################################

@when(
    'ssl-termination.installed',
    'lets-encrypt.registered',
    'reverseproxy.available')
def set_up():
    if not config.get('fqdn'):
        return
    reverseproxy = endpoint_from_flag('reverseproxy.available')
    services = reverseproxy.services()
    if not data_changed('sslterm.http', services) and \
       not config.changed('credentials'):
        return
    print('New http relation found, configuring proxy.')
    clean_nginx('/etc/nginx/sites-available/http')
    cert = lets_encrypt.live()

    upstreams = []
    for service in services:
        upstreams.extend(service['hosts'])

    create_nginx_config("/etc/nginx/sites-available/http/http-config",
                        [config.get("fqdn")],
                        upstreams,
                        "http-upstream",
                        cert,
                        config.get("credentials", ""),
                        "htaccess_http")
    update_nginx()
    set_flag('ssl-termination-http.setup')
    set_flag('ssl-termination.report')


@when(
    'ssl-termination.installed',
    'ssl-termination-http.setup'
)
@when_not(
    'reverseproxy.available'
)
def remove_http_setup():
    data_changed('sslterm.http', [])
    clean_nginx('/etc/nginx/sites-available/http')
    update_nginx()
    clear_flag('ssl-termination-http.setup')
    set_flag('ssl-termination.report')


########################################################################
# JuJu status handlers
########################################################################

@when('ssl-termination.report')
def report_ssl_status():
    registered_fqdns = []  
    cert_requests = unitdata.kv().get('sslterm.cert-requests', [])
    for cert_request in cert_requests:
        registered_fqdns.extend(cert_request['fqdn'])
    if config.get('fqdn') and is_flag_set('reverseproxy.available'):
        registered_fqdns.append(config.get('fqdn'))
    if registered_fqdns:
        status.active('Ready ({})'.format(",".join(registered_fqdns)))
    else:
        status.active('Ready')
    clear_flag('ssl-termination.report')

########################################################################
# Helper methods
########################################################################

def delete_old_certs(old_requests, new_requests):
    if not old_requests:
        return
    for request in new_requests:
        if request not in old_requests:
            for fqdn in request['fqdn']:
                if os.path.exists('/etc/letsencrypt/live/' + fqdn):
                    rmtree('/etc/letsencrypt/live/' + fqdn)
                    rmtree('/etc/letsencrypt/archive/' + fqdn)
                    os.remove('/etc/letsencrypt/renewal/' + fqdn + '.conf')


def create_nginx_config(abs_path, fqdn, upstreams, upstream_name, cert, credentials, htaccess_name):
    # fqdn has to be a list
    credentials = credentials.split()
    # Did we get a valid value? If not, blocked!
    if len(credentials) not in (0, 2):
        status.blocked('authentication config wrong! ' 
                       'I expect 2 space-separated string. I got {}.'.format(len(credentials)))
        return
    # We got a valid value, signal to regenerate config.
    try:
        os.remove('/etc/nginx/.' + htaccess_name)
    except OSError:
        pass

    nginx_context = {
        'privkey': cert['privkey'],
        'fullchain': cert['fullchain'],
        'fqdn': " ".join(fqdn),
        'upstreams': upstreams,
        'upstream_name': upstream_name,
        'dhparam': cert['dhparam'],
        'auth_basic': bool(credentials),
    }

    # Did we get credentials? If so, configure them.
    if len(credentials) == 2:
        check_call([
            'htpasswd', '-c', '-b', '/etc/nginx/.' + htaccess_name,
            credentials[0], credentials[1]])
        nginx_context['htpasswd'] = '/etc/nginx/.' + htaccess_name

    templating.render(source="encrypt.nginx.jinja2",
                      target=abs_path,
                      context=nginx_context)
    filename = abs_path.rstrip('/').split('/')[-1]
    os.symlink(abs_path, "/etc/nginx/sites-enabled/" + filename)


def clean_nginx(target):
    files = []
    for file in os.listdir(target):
        files.append(file)
    # Remove all symb links in /sites-enabled
    for file in os.listdir('/etc/nginx/sites-enabled'):
        if file in files:
            os.unlink('/etc/nginx/sites-enabled/' + file)
    # Remove all config files from /sites-available
    for file in os.listdir(target):
        os.remove(target.rstrip('/') + '/' + file)


def update_nginx():
    # Check if nginx config is valid
    try:
        cmd = run(['nginx', '-t'])
        cmd.check_returncode()
    except CalledProcessError as e:
        log(e)
        status.blocked('Invalid NGINX configuration')
        return False
    # Reload NGINX
    try:
        cmd = run(['nginx', '-s', 'reload'])
        cmd.check_returncode()
    except CalledProcessError as e:
        log(e)
        status.blocked('Error reloading NGINX')
        return False
    return True


def create_tcp_nginx_config(tcp_request, cert, juju_unit):
    """
    tcp_request: [{
                    'port': xxxx,
                    'hosts': ['x.x.x.x', 'x.x.x.x'],
                  }]
    """
    nginxcfg = NginxConfig()
    count = 0
    try:
        for tcp in tcp_request:
            name = "{}-{}".format(juju_unit, count)
            tcp_context = {
                'port': tcp['port'],
                'fullchain': cert['fullchain'],
                'privkey': cert['privkey'],
                'upstream_name': name,
                'upstreams': tcp['hosts'],
            }
            count += 1
            tcp_config = templating.render(source='streams.nginx.jinja2',
                                        target=None,
                                        context=tcp_context)
            nginxcfg.write_config(NginxModule.STREAM, tcp_config, name)
        nginxcfg.enable_all_config(NginxModule.STREAM) \
                .validate_nginx() \
                .reload_nginx()
    except NginxConfigError as e:
        log(e)
        status.blocked('{}'.format(e))
        return False
    return True
