from optparse import make_option

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from celery import chord, group

import amo
from addons.models import Addon
from amo.utils import chunked
from devhub.tasks import convert_purified, flag_binary, get_preview_sizes
from market.tasks import check_paypal, check_paypal_multiple

from mkt.webapps.tasks import (add_uuids, clean_apps, dump_apps,
                               fix_missing_icons, import_manifests,
                               update_developer_name, update_manifests,
                               update_supported_locales, zip_apps)


tasks = {
    # binary-components depend on having a chrome manifest.
    'flag_binary_components': {'method': flag_binary,
                               'qs': [Q(type__in=[amo.ADDON_EXTENSION,
                                                  amo.ADDON_DICT,
                                                  amo.ADDON_LPADDON,
                                                  amo.ADDON_PLUGIN,
                                                  amo.ADDON_API]),
                                      Q(disabled_by_user=False)],
                               'kwargs': dict(latest=False)},
    'flag_binary': {'method': flag_binary, 'qs': []},
    'get_preview_sizes': {'method': get_preview_sizes, 'qs': []},
    'convert_purified': {'method': convert_purified, 'qs': []},
    'check_paypal': {'pre': check_paypal_multiple,
                     'method': check_paypal,
                     'qs': [Q(premium_type=amo.ADDON_PREMIUM,
                              disabled_by_user=False),
                            ~Q(status=amo.STATUS_DISABLED)]},
    'update_manifests': {'method': update_manifests,
                         'qs': [Q(type=amo.ADDON_WEBAPP, is_packaged=False,
                                  status__in=[amo.STATUS_PENDING,
                                              amo.STATUS_PUBLIC,
                                              amo.STATUS_PUBLIC_WAITING],
                                  disabled_by_user=False)]},
    'add_uuids': {'method': add_uuids,
                  'qs': [Q(type=amo.ADDON_WEBAPP, guid=None),
                         ~Q(status=amo.STATUS_DELETED)]},
    'update_supported_locales': {
        'method': update_supported_locales,
        'qs': [Q(type=amo.ADDON_WEBAPP, disabled_by_user=False,
                 status__in=[amo.STATUS_PENDING, amo.STATUS_PUBLIC,
                             amo.STATUS_PUBLIC_WAITING])]},
    'dump_apps': {'method': dump_apps,
                  'qs': [Q(type=amo.ADDON_WEBAPP, status=amo.STATUS_PUBLIC,
                           disabled_by_user=False)],
                  'pre': clean_apps,
                  'post': zip_apps},
    'update_developer_name': {'method': update_developer_name,
                         'qs': [Q(type=amo.ADDON_WEBAPP,
                                  status__in=[amo.STATUS_PENDING,
                                              amo.STATUS_PUBLIC,
                                              amo.STATUS_PUBLIC_WAITING],
                                  disabled_by_user=False)]},
    'fix_missing_icons': {'method': fix_missing_icons,
                          'qs': [Q(type=amo.ADDON_WEBAPP,
                                  status__in=[amo.STATUS_PENDING,
                                              amo.STATUS_PUBLIC,
                                              amo.STATUS_PUBLIC_WAITING],
                                  disabled_by_user=False)]},
    'import_manifests': {'method': import_manifests,
                         'qs': [Q(type=amo.ADDON_WEBAPP,
                                  disabled_by_user=False)]},
}


class Command(BaseCommand):
    """
    A generic command to run a task on addons.
    Add tasks to the tasks dictionary, providing a list of Q objects if you'd
    like to filter the list down.

    method: the method to delay
    pre: a method to further pre process the pks, must return the pks (opt.)
    qs: a list of Q objects to apply to the method
    kwargs: any extra kwargs you want to apply to the delay method (optional)
    """
    option_list = BaseCommand.option_list + (
        make_option('--task', action='store', type='string',
                    dest='task', help='Run task on the addons.'),
    )

    def handle(self, *args, **options):
        task = tasks.get(options.get('task'))
        if not task:
            raise CommandError('Unknown task provided. Options are: %s'
                               % ', '.join(tasks.keys()))
        pks = (Addon.objects.filter(*task['qs'])
                            .values_list('pk', flat=True)
                            .order_by('-last_updated'))
        if 'pre' in task:
            # This is run in process to ensure its run before the tasks.
            pks = task['pre'](pks)
        if pks:
            kw = task.get('kwargs', {})
            # All the remaining tasks go in one group.
            grouping = []
            for chunk in chunked(pks, 100):
                grouping.append(
                    task['method'].subtask(args=[chunk], kwargs=kw))

            # Add the post task on to the end.
            post = None
            if 'post' in task:
                post = task['post'].subtask(args=[], kwargs=kw, immutable=True)
                ts = chord(grouping, post)
            else:
                ts = group(grouping)
            ts.apply_async()
