#
# core.py
#
# Copyright (C) 2014-2016 Omar Alvarez <osurfer3@hotmail.com>
# Copyright (C) 2011 Jamie Lennox <jamielennox@gmail.com>
#
# Basic plugin template created by:
# Copyright (C) 2008 Martijn Voncken <mvoncken@gmail.com>
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
# Copyright (C) 2009 Damien Churchill <damoxc@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
#   The Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor
#   Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#

from deluge.log import LOG as log
from deluge.plugins.pluginbase import CorePluginBase
import deluge.component as component
import deluge.configmanager
from deluge.core.rpcserver import export

from twisted.internet import reactor
from twisted.internet.task import LoopingCall, deferLater

import time

DEFAULT_PREFS = {
    'max_seeds': 0,
    'filter': 'func_ratio',
    'filter2': 'func_added',
    'count_exempt': False,
    'remove_data': False,
    'trackers': [],
    'labels': [],
    'min': 0.0,
    'min2': 0.0,
    'hdd_space': -1.0,
    'interval': 0.5,  # hours
    'sel_func': 'and',
    'remove': True,
    'enabled': False,
    'tracker_rules': {},
    'label_rules': {},
    'rule_1_enabled': True,
    'rule_2_enabled': True
}


def _get_ratio((i, t)):
    return t.get_ratio()

def _age_in_days((i, t)):
    now = time.time()
    added = t.get_status(['time_added'])['time_added']
    log.debug("_age_in_days(): Now = {}, added = {}".format(now, added))
    age_in_days = round((now - added) / 86400.0, 2)  # age in days
    log.debug("_age_in_days(): Returning age: {} (in days)".format(age_in_days))
    return age_in_days


def _date_added((i, t)):
    return (time.time() - t.time_added) / 86400.0


# Add key label also to get_remove_rules():141
filter_funcs = {
    'func_ratio': _get_ratio,
    #'func_added': lambda (i, t): round((time.time() - t.time_added) / 86400.0, 2),
    'func_added': _age_in_days,
    'func_seed_time': lambda (i, t):
        round(t.get_status(['seeding_time'])['seeding_time'] / 3600.0, 2),
    'func_seeders': lambda (i, t): t.get_status(['total_seeds'])['total_seeds']
}

sel_funcs = {
    'and': lambda (a, b): a and b,
    'or': lambda (a, b): a or b,
    'xor': lambda (a ,b): (a and not b) or (not a and b)
}


class Core(CorePluginBase):

    def enable(self):
        log.debug("AutoRemovePlus: Enabled")

        self.config = deluge.configmanager.ConfigManager(
            "autoremoveplus.conf",
            DEFAULT_PREFS
        )
        self.torrent_states = deluge.configmanager.ConfigManager(
            "autoremoveplusstates.conf",
            {}
        )

        # Safe after loading to have a default configuration if no gtkui
        self.config.save()
        self.torrent_states.save()

        # it appears that if the plugin is enabled on boot then it is called
        # before the torrents are properly loaded and so periodic_scan() receives an
        # empty list. So we must listen to SessionStarted for when deluge boots
        #  but we still have apply_now so that if the plugin is enabled
        # mid-program periodic_scan() is still run
        self.looping_call = LoopingCall(self.periodic_scan)
        deferLater(reactor, 5, self.start_looping)
        self.torrentmanager = component.get("TorrentManager")

    def disable(self):
        if self.looping_call.running:
            self.looping_call.stop()

    def update(self):
        pass

    def start_looping(self):
        log.warning('check interval loop starting')
        self.looping_call.start(self.config['interval'] * 3600.0)

    @export
    def set_config(self, config):
        """Sets the config dictionary"""
        for key in config.keys():
            self.config[key] = config[key]
        self.config.save()
        if self.looping_call.running:
            self.looping_call.stop()
        self.looping_call.start(self.config['interval'] * 3600.0)

    @export
    def get_config(self):
        """Returns the config dictionary"""
        return self.config.config

    @export
    def get_remove_rules(self):
        return {
            'func_ratio': 'Ratio',
            'func_added': 'Age in days',
            'func_seed_time': 'Seed Time (h)',
            'func_seeders': 'Seeders'
        }

    @export
    def get_ignore(self, torrent_ids):
        if not hasattr(torrent_ids, '__iter__'):
            torrent_ids = [torrent_ids]

        return [self.torrent_states.config.get(t, False) for t in torrent_ids]

    @export
    def set_ignore(self, torrent_ids, ignore=True):
        log.debug(
            "AutoRemovePlus: Setting torrents %s to ignore=%s"
            % (torrent_ids, ignore)
        )

        if not hasattr(torrent_ids, '__iter__'):
            torrent_ids = [torrent_ids]

        for t in torrent_ids:
            self.torrent_states[t] = ignore

        self.torrent_states.save()

    def check_min_space(self):
        min_hdd_space = self.config['hdd_space']
        real_hdd_space = component.get("Core").get_free_space() / 1073741824.0

        log.debug("Space: %s/%s" % (real_hdd_space, min_hdd_space))

        # if deactivated delete torrents
        if min_hdd_space < 0.0:
            return False

        # if hdd space below minimum delete torrents
        if real_hdd_space > min_hdd_space:
            return True  # there is enough space
        else:
            return False

    def pause_torrent(self, torrent):
        try:
            torrent.pause()
        except Exception, e:
            log.warn(
                "AutoRemovePlus: Problems pausing torrent: %s", e
            )

    def remove_torrent(self, tid, remove_data):
        try:
            self.torrentmanager.remove(tid, remove_data=remove_data)
            log.debug("remove_torrent(): successfully removed torrent: %s", tid)
        except Exception, e:
            log.warn(
                "remove_torrent(): AutoRemovePlus: Problems removing torrent: %s", e
            )
        try:
            del self.torrent_states.config[tid]
        except KeyError:
            return False
        else:
            return True

    def get_torrent_rules(self, id, torrent, tracker_rules, label_rules):

        total_rules = []

        try:
            for t in torrent.trackers:
                for name, rules in tracker_rules.iteritems():
                    log.debug("get_torrent_rules(): processing name = {}, rules = {}, url = {}, find = {} ".format(name, rules, t['url'], t['url'].find(name.lower())))
                    if(t['url'].find(name.lower()) != -1):
                        for rule in rules:
                            total_rules.append(rule)
        except Exception as e:
            log.warning("get_torrent_rules(): Exception with getting torrent rules for {}: {}".format(id, e))
            return total_rules

        if label_rules:
            try:
                # get label string
                label_str = component.get(
                    "CorePlugin.LabelPlus"
                ).get_torrent_label_name(id)

                # if torrent has labels check them
                labels = [label_str] if len(label_str) > 0 else []

                for label in labels:
                    if label in label_rules:
                        for rule in label_rules[label]:
                            total_rules.append(rule)
            except Exception as e:
                log.warning("get_torrent_rules(): Cannot obtain torrent label for {}: {}".format(id, e))

        log.debug("get_torrent_rules(): returning rules for {}: {}".format(id, total_rules))
        return total_rules

    # we don't use args or kwargs it just allows callbacks to happen cleanly
    def periodic_scan(self, *args, **kwargs):
        log.debug("AutoRemovePlus: starting periodic_scan()")

        max_seeds = int(self.config['max_seeds'])
        count_exempt = self.config['count_exempt']
        remove_data = self.config['remove_data']
        exemp_trackers = self.config['trackers']
        exemp_labels = self.config['labels']
        min_val = float(self.config['min'])
        min_val2 = float(self.config['min2'])
        remove = self.config['remove']
        enabled = self.config['enabled']
        tracker_rules = self.config['tracker_rules']
        rule_1_chk = self.config['rule_1_enabled']
        rule_2_chk = self.config['rule_2_enabled']
        labels_enabled = False

        if 'LabelPlus' in component.get(
            "CorePluginManager"
        ).get_enabled_plugins():
            labels_enabled = True
            label_rules = self.config['label_rules']
        else:
            log.debug("WARNING! LabelPlus plugin not active")
            log.debug("No labels will be checked for exemptions!")
            label_rules = []

        # Negative max means unlimited seeds are allowed, so don't do anything
        if max_seeds < 0:
            return

        torrent_ids = self.torrentmanager.get_torrent_list()

        log.debug("Number of torrents: {0}".format(len(torrent_ids)))

        # If there are less torrents present than we allow
        # then there can be nothing to do
        if len(torrent_ids) <= max_seeds:
            return

        torrents = []
        ignored_torrents = []

        # relevant torrents to us exist and are finished
        for i in torrent_ids:
            t = self.torrentmanager.torrents.get(i, None)

            # TODO: deluge2.0 version of this script doesn't have this try-ex-else block:
            # likely because the end of this function is way more convoluted/feature-packed than in this ver?
            try:
                finished = t.is_finished
            except Exception as e:
                log.warning("periodic_scan(): Cannot obtain torrent 'is_finished' attribute: [{}]".format(e))
                continue
            else:
                if not finished:
                    continue

            try:
                ignored = self.torrent_states[i]
            except KeyError as e:
                ignored = False

            ex_torrent = False
            trackers = t.trackers

            # check if trackers in exempted tracker list
            for tracker, ex_tracker in (
                (t, ex_t) for t in trackers for ex_t in exemp_trackers
            ):
                if(tracker['url'].find(ex_tracker.lower()) != -1):
                    log.debug("periodic_scan(): Found exempted tracker: %s" % (ex_tracker))
                    ex_torrent = True

            # check if labels in exempted label list if Label plugin is enabled
            if labels_enabled:
                try:
                    # get label string
                    label_str = component.get(
                        "CorePlugin.LabelPlus"
                    ).get_torrent_label_name(i)

                    # if torrent has labels check them
                    labels = [label_str] if len(label_str) > 0 else []

                    for label, ex_label in (
                        (l, ex_l) for l in labels for ex_l in exemp_labels
                    ):
                        if(label.find(ex_label.lower()) != -1):
                            log.debug("periodic_scan(): Found exempted label: %s" % (ex_label))
                            ex_torrent = True
                except Exception as e:
                    log.warning("periodic_scan(): Cannot obtain torrent label. [{}]".format(e))

            # if torrent tracker or label in exemption list, or torrent ignored
            # insert in the ignored torrents list
            (ignored_torrents if ignored or ex_torrent else torrents)\
                .append((i, t))

        log.debug("periodic_scan(): Number of finished torrents: {0}".format(len(torrents)))
        log.debug("periodic_scan(): Number of ignored torrents: {0}".format(len(ignored_torrents)))

        # now that we have trimmed active torrents
        # check again to make sure we still need to proceed
        if len(torrents) +\
                (len(ignored_torrents) if count_exempt else 0) <= max_seeds:
            return

        # if we are counting ignored torrents towards our maximum
        # then these have to come off the top of our allowance
        if count_exempt:
            max_seeds -= len(ignored_torrents)
            if max_seeds < 0:
                max_seeds = 0

        # Alternate sort by primary and secondary criteria
        torrents.sort(
            key=lambda x: (
                filter_funcs.get(
                    self.config['filter'],
                    _get_ratio
                )(x),
                filter_funcs.get(
                    self.config['filter2'],
                    _get_ratio
                )(x)
            ),
            reverse=False
        )

        changed = False

        # remove or pause these torrents
        for i, t in reversed(torrents[max_seeds:]):

            # check if free disk space below minimum
            if self.check_min_space():
                break  # break the loop, we have enough space

            log.debug(
                "periodic_scan(): AutoRemovePlus: Remove torrent %s, %s"
                % (i, t.get_status(['name'])['name'])
            )
            log.debug(
                filter_funcs.get(self.config['filter'], _get_ratio)((i, t))
            )
            log.debug(
                filter_funcs.get(self.config['filter2'], _get_ratio)((i, t))
            )
            if enabled:
                # Get result of first condition test
                filter_1 = filter_funcs.get(self.config['filter'], _get_ratio)((i, t)) >= min_val
                # Get result of second condition test
                filter_2 = filter_funcs.get(self.config['filter2'], _get_ratio)((i, t)) >= min_val2

                specific_rules = self.get_torrent_rules(i, t, tracker_rules, label_rules)

                # Sort rules according to logical operators, AND is evaluated first
                specific_rules.sort(key=lambda rule: rule[0])

                remove_cond = False

                # If there are specific rules, ignore general remove rules
                if specific_rules:
                    remove_cond = filter_funcs.get(specific_rules[0][1])((i, t)) >= specific_rules[0][2]
                    for rule in specific_rules[1:]:
                        check_filter = filter_funcs.get(rule[1])((i, t)) >= rule[2]
                        remove_cond = sel_funcs.get(rule[0])((
                            check_filter,
                            remove_cond
                        ))
                elif rule_1_chk and rule_2_chk:
                    # If both rules active use custom logical function
                    remove_cond = sel_funcs.get(self.config['sel_func'])((
                        filter_1,
                        filter_2
                    ))
                elif rule_1_chk and not rule_2_chk:
                    # Evaluate only first rule, since the other is not active
                    remove_cond = filter_1
                elif not rule_1_chk and rule_2_chk:
                    # Evaluate only second rule, since the other is not active
                    remove_cond = filter_2

                # If logical functions are satisfied remove or pause torrent
                if remove_cond:
                    if not remove:
                        self.pause_torrent(t)
                    else:
                        if self.remove_torrent(i, remove_data):
                            changed = True

        # If a torrent exemption state has been removed save changes
        if changed:
            self.torrent_states.save()
