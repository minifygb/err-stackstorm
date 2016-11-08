#coding:utf-8
from errbot import BotPlugin, re_botcmd, botcmd, webhook
from st2client.client import Client
import subprocess
import copy
import re
import logging
import six
import time
try:
    import urlparse
except :
    import urllib.parse as urlparse
import requests, json
from requests.auth import HTTPBasicAuth


class St2(BotPlugin):
    """
    Stackstorm plugin for authentication and Action Alias execution.  Try !st2help for action alias help.
    """
    def __init__(self, bot):
        super(St2, self).__init__(bot)
        # We append 'st2 ' to the bot prefix to avoid action alias definitions
        # from colliding with errbot's native plugin commands.
        self.bot_prefix = "{}st2 ".format(self.bot_config.BOT_PREFIX)
        self.st2_config = self.bot_config.STACKSTORM
        self.base_url = self.st2_config.get('base_url') or 'http://localhost'
        self.auth_url = self.st2_config.get('auth_url') or 'http://localhost:9100'
        self.api_url = self.st2_config.get('api_url') or 'http://localhost:9100/v1'
        self.api_version = self.st2_config.get('api_version') or 'v1'
        self.timer_update = self.st2_config.get('timer_update') or 60

        # API user authentication.
        api_auth = self.st2_config.get('api_auth') or {}
        tmp_user = api_auth.get('user') or None
        if tmp_user:
            self.api_username = tmp_user.get('name') or None
            self.api_password = tmp_user.get('password') or None
            self.api_token = tmp_user.get("token") or None

        # API Key support doesn't exist in st2client as of version 1.4
        # but it's being worked on # so it's provisioned here for future
        # use.
        self.api_key = api_auth.get('key') or None

        self.pattern_action = {}
        self.help = ''  #show help doc with exec `!helpst2`
        self.tolerant_gen_patterns_and_help()



    def activate(self):
        """
        Enable poller to fetch st2 action alias patterns and help
        """
        super().activate()
        self.start_poller(self.timer_update, self.tolerant_gen_patterns_and_help)


    @re_botcmd(pattern=r'^st2 .*')
    def st2_run(self, msg, match):
        """
        Run an arbitrary stackstorm command.
        Available commands can be listed using !st2help
        """
        _msg = unicode(msg)
        data = self.match(_msg)
        logging.info("st2 matched with the following %s" % data)
        if data:
            action_ref = data.pop('action_ref')
            logging.info('st2 run request %s : %s' % (_msg, action_ref))
            res = self.run_action(action_ref, **data["kwargs"])
            logging.info('st2 run response: {0}'.format(res))
            return res
        else:
            return "st2 command not found '{}'.  Check help with !st2help".format(_msg)


    @botcmd
    def st2help(self, msg, args):
        """
        Provide help for stackstorm action aliases.
        """
        return self.help


    @webhook('/chatops/message')
    def chatops_message(self, request):
        """
        Webhook entry point for stackstorm to post messages into
        errbot which will relay them into the chat backend.
        """
        logging.info(request)

        channel = request['channel']
        message = request['message']

        user = request.get('user') or None
        whisper = request.get('whisper') or None

        self.send(
            self.build_identifier(channel),
            message,
        )
        truncated = ""
        if len(message) > 96:
            truncated = " ..."
        logging.info("'{}{}'".format(message[:97], truncated))
        return "Message Received."



    def _trial_token(self):
        """
        Send token header 'X-Auth-Token: <token>'
        to API endpoint https://<stackstorm host>/api/'
        """
        add_headers = {'X-Auth-Token': self.api_token}
        r = self._http_request('GET', '/api/', headers=add_headers)
        if r.status_code == 401: # unauthorised try to get a new token.
            self._renew_token()
        else:
            logging.info('API response to token = {} {}'.format(r.status_code, r.reason))



    def _renew_token(self):
        """
        Request a new user token be created by stackstorm and use it
        to query the API end point.
        """
        auth = HTTPBasicAuth(self.api_username, self.api_password)
        r = self._http_request('POST', '/auth/{}/tokens'.format(self.api_version), auth=auth)

        if r.status_code == 201: # created.
            auth_info = r.json()
            self.api_token = auth_info["token"]
            logging.info("Received new token %s" % self.api_token)
        else:
            logging.warning('Failed to get new user token. {} {}'.format(r.status_code, r.reason))



    def _http_request(self, verb="GET", url="/", headers={}, auth=None):
        """
        Generic HTTP call.
        """
        get_kwargs = {
            'headers': headers,
            'timeout': 5,
            'verify': False
        }

        if auth:
            get_kwargs['auth'] = auth

        host = self.base_url.rsplit('//')[1]
        response = requests.request(verb, 'https://{}{}'.format(host,url), **get_kwargs)

        return response



    def run_action(self, action, **kwargs):
        """
        Perform the system call to execute the Stackstorm action.
        """
        # This method ties errbot to the same machine as the stackstorm
        # installation.  TO DO: Investigate if errbot can execute stackstorm
        # runs from a remote host.
        cmd = [ '/opt/stackstorm/st2/bin/st2',
                '--url={}'.format(self.base_url),
                '--auth-url={}'.format(self.auth_url),
                '--api-url={}'.format(self.api_url),
                '--api-version={}'.format(self.api_version),
                'run',
                '-j',
                '-t',
                '{}'.format(self.api_token),
                '{}'.format(action),
        ]
        for k, v in six.iteritems(kwargs):
            cmd.append('{}={}'.format(k, v))

        sp = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd='/opt/stackstorm/st2/bin')
        output = sp.communicate()[0].strip().decode('utf-8')
        returncode = sp.returncode
        logging.info(output)
        output = self._format_output(output)

        return output



    def _format_output(self, output):
        exec_data = json.loads(output)
        return '`{} {}` ```{}```'.format(exec_data['id'], exec_data['status'], exec_data['result'])



    def tolerant_gen_patterns_and_help(self):
        """
        A wrapper method to check for API access authorisation.
        """
        try:
            self.gen_patterns_and_help()
        except requests.exceptions.HTTPError as e:
            # Attempt to re-authenticate on HTTP Error
            self._trial_token()
            self.gen_patterns_and_help()
        except Exception as e:
            logging.error("Error while fetching action aliases %s" % e.message)


    def gen_patterns_and_help(self):
        """
        gen pattern and help for action alias
        """
        self.help = ''
        self.pattern_action = {}

        st2_client = Client(base_url=self.base_url, api_url=self.api_url, token=self.api_token)

        for alias_obj in st2_client.managers['ActionAlias'].get_all():
            for _format in alias_obj.formats:
				
                display, representations = self._normalise_format(_format)
                for representation in representations:
                    if not ( isinstance(representation, str) or isinstance(representation, unicode) ):
                        logging.info("Skipping: %s which is type %s" % (alias_obj.action_ref, type(representation)))
                        continue
                    pattern_context, kwargs = self._format_to_pattern(representation)
                    self.pattern_action[pattern_context] = {
                        "action_ref": alias_obj.action_ref,
                        "kwargs": kwargs
                    }
                    self.help += '{}{} -- {}\r\n'.format(self.bot_prefix, display, alias_obj.description)
        if self.help == '':
            self.help = 'No Action-Alias definitions were found.  No help is available.'



    def _normalise_format(self, alias_format):
        """
        Stackstorm action aliases can have two types;
            1. A simple string holding the format
            2. A dictionary which hold numberous alias format "representation(s)"
               With a single "display" for help about the action alias.
        This function processes both forms and returns a standardised form.
        """
        display = None
        representation = []
        if isinstance(alias_format, str) or isinstance(alisa_format, unicode):
            display = alias_format
            representation.append(alias_format)
        if isinstance(alias_format, dict):
            display = alias_format['display']
            representation = alias_format['representation']
        return (display, representation)



    def _format_to_pattern(self, alias_format):
        """
        Extract named arguments from format to create a keyword argument list.
        Transform tokens into regular expressions.
        """
        kwargs = {}
        # Step 1: Extract action alias arguments so they can be used later
        #         when calling the stackstorm action.
        tokens = re.findall(r"{{(.*?)}}", alias_format, re.IGNORECASE)
        for token in tokens:
            if token.find("=") > -1:
                name, val = token.split("=")
                # Remove unnecessary whitespace
                name = name.strip()
                val = val.strip()
                kwargs[name] = val
                name = r"?P<{}>[\s\S]+?".format(name)
            else:
                name = token.strip()
                kwargs[name] = None
                name = r"?P<{}>[\s\S]+?".format(name)
            # The below code causes a regex exception to be raised under certain conditions.  Using replace() as alternative.
            #~ alias_format = re.sub( r"\s*{{{{{}}}}}\s*".format(token), r"\\s*({})\\s*".format(name), alias_format)
            # Replace token with named group match.
            alias_format = alias_format.replace(r"{{{{{}}}}}".format(token), r"({})".format(name))


        # Step 2: Append regex to match any extra parameters that weren't declared in the action alias.
        extra_params = r"""(:?\s+(\S+)\s*=("([\s\S]*?)"|'([\s\S]*?)'|({[\s\S]*?})|(\S+))\s*)*"""
        alias_format = r'^{}{}{}$'.format(self.bot_prefix, alias_format, extra_params)

        return (re.compile(alias_format, re.I), kwargs)



    def _extract_extra_params(self, extra_params):
        """
        Returns a dictionary of extra parameters supplied in the action_alias.
        """
        kwargs = {}
        for arg in extra_params.groups():
            if arg and "=" in arg:
                k, v = arg.split("=", 1)
                kwargs[k.strip()] = v.strip()
        return kwargs



    def match(self, text):
        """
        Match the text against an action and return the action reference.
        """
        results = []
        for pattern in self.pattern_action:
            res = pattern.search(text)
            if res:
                data = {}
                # Create keyword arguments starting with the defaults.
                # Deep copy is used here to avoid exposing the reference
                # outside the match function.
                data.update(copy.deepcopy(self.pattern_action[pattern]))
                # Merge in the named arguments.
                data["kwargs"].update(res.groupdict())
                # Merge in any extra arguments supplied as a key/value pair.
                data["kwargs"].update(self._extract_extra_params(res))
                results.append(data)

        if not results:
            return None

        results.sort(reverse=True)
        return results[0]
