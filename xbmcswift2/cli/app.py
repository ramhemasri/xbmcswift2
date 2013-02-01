'''
    xbmcswift2.cli.app
    ----------------

    This package contains the code which runs plugins from the command line.

    :copyright: (c) 2012 by Jonathan Beluch
    :license: GPLv3, see LICENSE for more details.
'''
import os
import sys
import logging
from xml.etree import ElementTree as ET

from xbmcswift2 import Plugin, ListItem, logger
from xbmcswift2.common import Modes
from xbmcswift2.cli import Option
from xbmcswift2.cli.console import (display_listitems, continue_or_quit,
    get_user_choice)


class RunCommand(object):
    '''A CLI command to run a plugin.'''

    command = 'run'
    usage = ('%prog run [once|interactive|crawl] [url]')
    option_list = (
        Option('-q', '--quiet', action='store_true',
               help='set logging level to quiet'),
        Option('-v', '--verbose', action='store_true',
               help='set logging level to verbose'),
    )

    @staticmethod
    def run(opts, args):
        '''The run method for the 'run' command. Executes a plugin from the
        command line.
        '''
        setup_options(opts)

        mode = Modes.ONCE
        if len(args) > 0 and hasattr(Modes, args[0].upper()):
            _mode = args.pop(0).upper()
            mode = getattr(Modes, _mode)

        url = None
        if len(args) > 0:
            # A url was specified
            url = args.pop(0)

        plugin_mgr = PluginManager.load_plugin_from_addonxml(mode, url)
        plugin_mgr.run()


def setup_options(opts):
    '''Takes any actions necessary based on command line options'''
    if opts.quiet:
        logger.log.setLevel(logging.WARNING)
        logger.GLOBAL_LOG_LEVEL = logging.WARNING

    if opts.verbose:
        logger.log.setLevel(logging.DEBUG)
        logger.GLOBAL_LOG_LEVEL = logging.DEBUG


def get_addon_module_name(addonxml_filename):
    '''Attempts to extract a module name for the given addon's addon.xml file.
    Looks for the 'xbmc.python.pluginsource' extension node and returns the
    addon's filename without the .py suffix.
    '''
    try:
        xml = ET.parse(addonxml_filename).getroot()
    except IOError:
        sys.exit('Cannot find an addon.xml file in the current working '
                 'directory. Please run this command from the root directory '
                 'of an addon.')

    try:
        plugin_source = (ext for ext in xml.findall('extension') if
                         ext.get('point') == 'xbmc.python.pluginsource').next()
    except StopIteration:
        sys.exit('ERROR, no pluginsource in addonxml')

    return plugin_source.get('library').split('.')[0]


class PluginManager(object):
    '''A class to handle running a plugin in CLI mode. Handles setup state
    before calling plugin.run().
    '''

    @classmethod
    def load_plugin_from_addonxml(cls, mode, url):
        '''Attempts to import a plugin's source code and find an instance of
        :class:`~xbmcswif2.Plugin`. Returns an instance of PluginManager if
        succesful.
        '''
        cwd = os.getcwd()
        sys.path.insert(0, cwd)
        module_name = get_addon_module_name(os.path.join(cwd, 'addon.xml'))
        addon = __import__(module_name)

        # Find the first instance of xbmcswift2.Plugin
        try:
            plugin = (attr_value for attr_value in vars(addon).values()
                      if isinstance(attr_value, Plugin)).next()
        except StopIteration:
            sys.exit('Could\'t find a Plugin instance in %s.py' % module_name)

        return cls(plugin, mode, url)

    def __init__(self, plugin, mode, url):
        self.plugin = plugin
        self.mode = mode
        self.url = url

    def run(self):
        '''This method runs the the plugin in the appropriate mode parsed from
        the command line options.
        '''
        handle = 0
        handlers = {
           Modes.ONCE: once,
           Modes.CRAWL: crawl,
           Modes.INTERACTIVE: interactive,
        }
        handler = handlers[self.mode]
        patch_sysargv(self.url or 'plugin://%s/' % self.plugin.id, handle)
        return handler(self.plugin)


def patch_sysargv(*args):
    '''Patches sys.argv with the provided args'''
    sys.argv = args[:]


def patch_plugin(plugin, path, handle=None):
    '''Patches a few attributes of a plugin instance to enable a new call to
    plugin.run()
    '''
    if handle is None:
        handle = plugin.request.handle
    patch_sysargv(path, handle)
    plugin._end_of_directory = False


def once(plugin, parent_item=None):
    '''A run mode for the CLI that runs the plugin once and exits.'''
    plugin.clear_added_items()
    items = plugin.run()

    # Prepend the parent_item if given
    if parent_item is not None:
        items.insert(0, parent_item)

    display_listitems(items)
    return items


def interactive(plugin):
    '''A run mode for the CLI that runs the plugin in a loop based on user
    input.
    '''
    items = [item for item in once(plugin) if not item.get_played()]
    parent_stack = []  # Keep track of parents so we can have a '..' option

    selected_item = get_user_choice(items)
    while selected_item is not None:
        if parent_stack and selected_item == parent_stack[-1]:
            if plugin._update_listing:
                # We have already put the last update_listing=False item on the
                # stack since we won't know if there will be a future usage of
                # update_listing=True. The correct parent item is actually the
                # parent of the currently selected_item, or 2 down the stack.

                # TODO: Account for jumping into a plugin with a route that
                # uses update_listing=True. There won't be two items on the
                # stack.
                parent_stack.pop()  # remove the incorrect parent

                # reassign selected_item to the correct parent
                selected_item = parent_stack.pop()
            else:
                # User selected the parent item, remove from list
                parent_stack.pop()
        elif plugin._update_listing:
            # since update_listing=True, we do not add this url to the parent
            # stack.
            pass
        else:
            # User selected non parent item, add current url to parent stack
            parent_stack.append(ListItem.from_dict(label='..',
                                                   path=plugin.request.url))
        patch_plugin(plugin, selected_item.get_path())

        # If we have parent items, include the top of the stack in the list
        # item display
        parent_item = None
        if parent_stack:
            parent_item = parent_stack[-1]
        items = [item for item in once(plugin, parent_item=parent_item)
                 if not item.get_played()]
        selected_item = get_user_choice(items)


def crawl(plugin):
    '''Performs a breadth-first crawl of all possible routes from the
    starting path. Will only visit a URL once, even if it is referenced
    multiple times in a plugin. Requires user interaction in between each
    fetch.
    '''
    # TODO: use OrderedSet?
    paths_visited = set()
    paths_to_visit = set(item.get_path() for item in once(plugin))

    while paths_to_visit and continue_or_quit():
        path = paths_to_visit.pop()
        paths_visited.add(path)

        # Run the new listitem
        patch_plugin(plugin, path)
        new_paths = set(item.get_path() for item in once(plugin))

        # Filter new items by checking against urls_visited and
        # urls_tovisit
        paths_to_visit.update(path for path in new_paths
                              if path not in paths_visited)
