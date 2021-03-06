#!/usr/bin/python
import sys, os, shutil, re
from datetime import datetime, timedelta
from time import time, localtime
from calendar import timegm
from argparse import ArgumentParser, SUPPRESS

import logging
import logging.handlers
import subprocess

import pygtk
pygtk.require('2.0')
import gtk, glib


def main():
    """
    Run power management operation as many times as needed
    """
    args, extra_args = MyArgumentParser().parse()

    # Verify that script is run as root
    if os.getuid():
        sys.stderr.write('This script needs superuser '
                         'permissions to run correctly\n')
        sys.exit(1)

    # Verify wakeup alarm can be scheduled
    if args.pm_operation != 'reboot':
        WakeUpAlarm.check()

    LoggingConfiguration.set(args.log_level, args.log_filename, args.append)
    logging.debug('Arguments: {0!r}'.format(args))
    logging.debug('Extra Arguments: {0!r}'.format(extra_args))

    # Log deprecation warning
    if args.pm_operation in ('suspend', 'hibernate'):
        logging.warning('{pm_operation!r} test case will be replaced '
                        'with a new one based on fwts'
                        .format(pm_operation=args.pm_operation))

    try:
        operation = PowerManagementOperation(args, extra_args)
        operation.setup()
        operation.run()
    except (TestCancelled, TestFailed) as exception:
        operation.teardown()
        if isinstance(exception, TestFailed):
            logging.error(exception.args[0])
        message = exception.MESSAGE.format(args.pm_operation.capitalize())
        if args.silent:
            logging.info(message)
        else:
            title = '{0} test'.format(args.pm_operation.capitalize())
            MessageDialog(title, message, gtk.MESSAGE_ERROR).run()

        return exception.RETURN_CODE

    return 0


class PowerManagementOperation(object):
    SLEEP_TIME = 5

    def __init__(self, args, extra_args):
        self.args = args
        self.extra_args = extra_args


    def setup(self):
        """
        Enable configuration file
        """
        if self.args.pm_operation in ('reboot', 'poweroff'):
            # Enagle autologin and sudo on first cycle
            if self.args.total == self.args.repetitions:
                AutoLoginConfigurator().enable()
                SudoersConfigurator().enable()

            # Schedule this script to be automatically executed
            # on startup to continue testing
            autostart_file = AutoStartFile(self.args)
            autostart_file.write()


    def run(self):
        if self.args.pm_operation in ('reboot', 'poweroff'):
            self.run_one_test_cycle()
        else:
            self.run_multiple_test_cycles()


    def run_multiple_test_cycles(self):
        """
        Run multiple power management iterations
        """
        # Perform as many cycles as required
        while self.args.repetitions >= 0:
            self.run_one_test_cycle()
            self.args.repetitions -= 1


    def run_one_test_cycle(self):
        """
        Run a power management iteration
        """
        logging.info('{0} operations remaining: {1}'
                     .format(self.args.pm_operation, self.args.repetitions))

        self.check_last_cycle_duration()
        if self.args.repetitions > 0:
            self.run_pm_command()
        else:
            self.summary()


    def check_last_cycle_duration(self):
        """
        Make sure that last cycle duration was reasonable,
        that is, not too short, not too long
        """
        min_pm_time = timedelta(seconds=self.args.min_pm_time)
        max_pm_time = timedelta(seconds=self.args.max_pm_time)
        if self.args.pm_timestamp:
            pm_timestamp = datetime.fromtimestamp(self.args.pm_timestamp)
            now = datetime.now()
            pm_time = now - pm_timestamp
            if pm_time < min_pm_time:
                raise TestFailed('{0} time less than expected: {1} < {2}'
                                 .format(self.args.pm_operation.capitalize(),
                                      pm_time, min_pm_time))
            if pm_time > max_pm_time:
                raise TestFailed('{0} time greater than expected: {1} > {2}'
                                 .format(self.args.pm_operation.capitalize(),
                                         pm_time, max_pm_time))

            logging.info('{0} time: {1}'
                         .format(self.args.pm_operation.capitalize(), pm_time))


    def run_pm_command(self):
        """
        Run power managment command and check result if needed
        """
        # Display information to user
        # and make it possible to cancel the test
        CountdownDialog(self.args.pm_operation,
                        self.args.pm_delay,
                        self.args.hardware_delay,
                        self.args.total-self.args.repetitions,
                        self.args.total).run()

        if self.args.pm_operation in ('suspend', 'hibernate'):
            command_str = 'pm-{0}'.format(self.args.pm_operation)
        else:
            # A small sleep time is added to reboot and poweroff
            # so that script has time to return a value
            # (useful when running it as an automated test)
            command_str = ('sleep {0}; {1}'
                           .format(self.SLEEP_TIME, self.args.pm_operation))
        if self.extra_args:
            command_str += ' {0}'.format(' '.join(self.extra_args))

        if self.args.pm_operation != 'reboot':
            WakeUpAlarm.set(seconds=self.args.wakeup)

        logging.info('Executing new {0!r} operation...'
                     .format(self.args.pm_operation))
        if self.args.pm_operation in ('suspend', 'hibernate'):
            command = Command(command_str, verbose=False).run()
            min_pm_time = timedelta(seconds=self.args.min_pm_time)
            max_pm_time = timedelta(seconds=self.args.max_pm_time)
            if command.time < min_pm_time:
                raise TestFailed('{0} time less than expected: {1} < {2}'
                                 .format(self.args.pm_operation.capitalize(),
                                         command.time, min_pm_time))
            if command.time > max_pm_time:
                raise TestFailed('{0} time greater than expected: {1} > {2}'
                                 .format(self.args.pm_operation.capitalize(),
                                         command.time, max_pm_time))

            logging.info('{0} time: {1}'
                         .format(self.args.pm_operation.capitalize(),
                                 command.time))
        else:
            logging.debug('Executing: {0!r}...'.format(command_str))
            subprocess.Popen(command_str, shell=True)


    def summary(self):
        """
        Gather hardware information for the last time,
        log execution time and exit
        """
        # Just gather hardware information one more time and exit
        CountdownDialog(self.args.pm_operation,
                        self.args.pm_delay,
                        self.args.hardware_delay,
                        self.args.total-self.args.repetitions,
                        self.args.total).run()

        self.teardown()

        # Log some time information
        start = datetime.fromtimestamp(self.args.start)
        end = datetime.now()
        if self.args.pm_operation == 'reboot':
            sleep_time=timedelta(seconds=self.SLEEP_TIME)
        else:
            sleep_time=timedelta(seconds=self.args.wakeup)

        wait_time=timedelta(seconds=(self.args.pm_delay
                                     +self.args.hardware_delay)*self.args.total)
        average = (end-start-wait_time)/self.args.total-sleep_time
        time_message = ('Total elapsed time: {total}\n'
                        'Average recovery time: {average}'
                        .format(total=end-start,
                                average=average))
        logging.info(time_message)

        message = '{0} test complete'.format(self.args.pm_operation.capitalize())
        if self.args.silent:
            logging.info(message)
        else:
            title = '{0} test'.format(self.args.pm_operation.capitalize())
            MessageDialog(title, message).run()

    def teardown(self):
        """
        Restore configuration
        """
        if self.args.pm_operation in ('reboot', 'poweroff'):
            # Don't execute this script again on next reboot
            autostart_file = AutoStartFile(self.args)
            autostart_file.remove()

            # Restore previous configuration
            SudoersConfigurator().disable()
            AutoLoginConfigurator().disable()


class TestCancelled(Exception):
    RETURN_CODE = 1
    MESSAGE = '{0} test cancelled by user'


class TestFailed(Exception):
    RETURN_CODE = 2
    MESSAGE = '{0} test failed'


class FWTSDialog(gtk.Dialog):
    """
    Run fwts wakealarm test
    and pulse a progress bar
    """
    def __init__(self):
        super(FWTSDialog, self).__init__('fwts wakealarm')
        self.set_resizable(False)
        self.set_deletable(False)

        alignment = gtk.Alignment(0.5, 0.5, 1.0, 0.1)
        alignment.set_padding(10, 10, 10, 10)
        self.vbox.pack_start(alignment)

        progress_bar = gtk.ProgressBar()
        progress_bar.set_text('Checking wakeup alarm...')
        alignment.add(progress_bar)

        self.show_all()

        self.progress_bar = progress_bar
        self.process = subprocess.Popen('fwts wakealarm --stdout-summary',
                                        shell=True, stdout=subprocess.PIPE)


    def run(self):
        """
        Run fwts in a separate process
        and check periodically if it has already returned
        """
        glib.timeout_add(250, self.on_timeout_cb)
        super(FWTSDialog, self).run()
        self.destroy()

        return self.process.stdout.read().splitlines()[-1]


    def on_timeout_cb(self):
        """
        Poll for fwts process output and pulse progress bar if needed
        """
        returncode = self.process.poll()
        if returncode is None:
            self.progress_bar.pulse()
            return True

        self.response(gtk.RESPONSE_ACCEPT)
        return False


class WakeUpAlarm(object):
    ALARM_FILENAME = '/sys/class/rtc/rtc0/wakealarm'
    RTC_FILENAME = '/proc/driver/rtc'

    @classmethod
    def check(cls):
        # Verify that wakeup related files exist
        if not os.path.isfile(cls.ALARM_FILENAME):
            sys.stderr.write('Alarm file ({0!r}) not found\n'
                             .format(cls.ALARM_FILENAME))
            sys.exit(1)

        if not os.path.isfile(cls.RTC_FILENAME):
            sys.stderr.write('RTC file ({0!r}) not found\n'
                             .format(cls.RTC_FILENAME))
            sys.exit(1)

        dialog = FWTSDialog()
        if dialog.run() != 'PASSED':
            sys.stderr.write('FWTS wakealarm test failed\n')
            sys.exit(1)


    @classmethod
    def set(cls, minutes=0, seconds=0):
        """
        Calculate wakeup time and write it to BIOS
        """
        now = int(time())
        timeout = minutes * 60 + seconds
        wakeup_time_utc = now + timeout
        wakeup_time_local = timegm(localtime()) + timeout

        subprocess.check_call('echo 0 > %s' % cls.ALARM_FILENAME, shell=True)
        subprocess.check_call('echo %d > %s' % (wakeup_time_utc, cls.ALARM_FILENAME), shell=True)

        with open(cls.ALARM_FILENAME) as alarm_file:
            wakeup_time_stored_str = alarm_file.read()

            if not re.match('\d+', wakeup_time_stored_str):
                subprocess.check_call('echo "+%d" > %s' % (timeout, cls.ALARM_FILENAME), shell=True)
                with open(cls.ALARM_FILENAME) as alarm_file2:
                    wakeup_time_stored_str = alarm_file2.read()
                if not re.match('\d+', wakeup_time_stored_str):
                    logging.error('Invalid wakeup time format: {0!r}'
                                  .format(wakeup_time_stored_str))
                    sys.exit(1)

            wakeup_time_stored =  int(wakeup_time_stored_str)
            try:
                logging.debug('Wakeup timestamp: {0} ({1})'
                              .format(wakeup_time_stored,
                                      datetime.fromtimestamp(wakeup_time_stored).strftime('%c')))
            except ValueError as e:
                logging.error(e)
                sys.exit(1)

            if ((abs(wakeup_time_utc - wakeup_time_stored) > 1) and
                (abs(wakeup_time_local - wakeup_time_stored) > 1)):
                logging.error('Wakeup time not stored correctly')
                sys.exit(1)

        with open(cls.RTC_FILENAME) as rtc_file:
            separator_regex = re.compile('\s+:\s+')
            rtc_data = dict([separator_regex.split(line.rstrip())
                             for line in rtc_file])
            logging.debug('RTC data:\n{0}'
                          .format('\n'.join(['- {0}: {1}'.format(*pair)
                                             for pair in rtc_data.items()])))

            # Verify wakeup time has been set properly
            # by looking into the alarm_IRQ and alrm_date field
            if rtc_data['alarm_IRQ'] != 'yes':
                logging.error('alarm_IRQ not set properly: {0}'
                              .format(rtc_data['alarm_IRQ']))
                sys.exit(1)


            if '*' in rtc_data['alrm_date']:
                logging.error('alrm_date not set properly: {0}'
                              .format(rtc_data['alrm_date']))
                sys.exit(1)


class Command(object):
    """
    Simple subprocess.Popen wrapper to run shell commands
    and log their output
    """
    def __init__(self, command_str, verbose=True):
        self.command_str = command_str
        self.verbose = verbose

        self.process = None
        self.stdout = None
        self.stderr = None
        self.time = None

    def run(self):
        """
        Execute shell command and return output and status
        """
        logging.debug('Executing: {0!r}...'.format(self.command_str))

        self.process = subprocess.Popen(self.command_str,
                                        shell=True,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        start = datetime.now()
        result = self.process.communicate()
        end = datetime.now()
        self.time = end-start

        if self.verbose:
            stdout, stderr = result
            message = ['Output:\n'
                       '- returncode:\n{0}'.format(self.process.returncode)]
            if stdout:
                message.append('- stdout:\n{0}'.format(stdout))
            if stderr:
                message.append('- stderr:\n{0}'.format(stderr))
            logging.debug('\n'.join(message))

            self.stdout = stdout
            self.stderr = stderr

        return self


class CountdownDialog(gtk.Dialog):
    """
    Dialog that shows the amount of progress in the reboot test
    and lets the user cancel it if needed
    """
    def __init__(self, pm_operation, pm_delay, hardware_delay, iterations, iterations_count):
        self.pm_operation = pm_operation
        title = '{0} test'.format(pm_operation.capitalize())

        buttons=(gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,)
        super(CountdownDialog, self).__init__(title=title,
                                              buttons=buttons)
        self.set_default_response(gtk.RESPONSE_CANCEL)
        self.set_resizable(False)
        self.set_position(gtk.WIN_POS_CENTER)

        progress_bar = gtk.ProgressBar()
        progress_bar.set_fraction(iterations/float(iterations_count))
        progress_bar.set_text('{0}/{1}'
                              .format(iterations, iterations_count))
        self.vbox.pack_start(progress_bar)

        operation_event = {'template': ('Next {0} in {{time}} seconds...'
                                        .format(self.pm_operation)),
                           'timeout': pm_delay}
        hardware_info_event = \
            {'template': 'Gathering hardware information in {time} seconds...',
             'timeout': hardware_delay,
             'callback': self.on_hardware_info_timeout_cb}

        if iterations == 0:
            # In first iteration, gather hardware information directly
            # and perform pm-operation
            self.on_hardware_info_timeout_cb()
            self.events = [operation_event]
        elif iterations < iterations_count:
            # In last iteration, wait before gathering hardware information
            # and perform pm-operation
            self.events = [operation_event,
                           hardware_info_event]
        else:
            # In last iteration, wait before gathering hardware information
            # and finish the test
            self.events = [hardware_info_event]

        self.label = gtk.Label()
        self.vbox.pack_start(self.label)
        self.show_all()


    def run(self):
        """
        Set label text and run dialog
        """
        self.schedule_next_event()
        response = super(CountdownDialog, self).run()
        self.destroy()

        if response != gtk.RESPONSE_ACCEPT:
            raise TestCancelled()


    def schedule_next_event(self):
        """
        Schedule next timed event
        """
        if self.events:
            self.event = self.events.pop()
            self.timeout_counter = self.event.get('timeout', 0)
            self.label.set_text(self.event['template']
                                .format(time=self.timeout_counter))
            glib.timeout_add_seconds(1, self.on_timeout_cb)
        else:
            # Return Accept response
            # if there are no other events scheduled
            self.response(gtk.RESPONSE_ACCEPT)


    def on_timeout_cb(self):
        """
        Set label properly and use callback method if needed
        """
        if self.timeout_counter > 0:
            self.label.set_text(self.event['template']
                                .format(time=self.timeout_counter))
            self.timeout_counter -= 1
            return True

        # Call calback if defined
        callback = self.event.get('callback')
        if callback:
            callback()

        # Schedule next event if needed
        self.schedule_next_event()

        return False


    def on_hardware_info_timeout_cb(self):
        """
        Gather hardware information and print it to logs
        """
        logging.info('Gathering hardware information...')
        logging.debug('Networking:\n'
                      '{network}\n'
                      '{ethernet}\n'
                      '{ifconfig}\n'
                      '{iwconfig}'
                      .format(network=Command('lspci | grep Network').run().stdout,
                              ethernet=Command('lspci | grep Ethernet').run().stdout,
                              ifconfig=Command("ifconfig -a | grep -A1 '^\w'").run().stdout,
                              iwconfig=Command("iwconfig | grep -A1 '^\w'").run().stdout))
        logging.debug('Bluetooth Device:\n'
                      '{hciconfig}'.format(hciconfig=Command("hciconfig -a | grep -A2 '^\w'").run().stdout))
        logging.debug('Video Card:\n'
                      '{lspci}'
                      .format(lspci=Command('lspci | grep VGA').run().stdout))
        logging.debug('Touchpad and Keyboard:\n'
                      '{xinput}'
                      .format(xinput=Command('xinput list').run().stdout))
        logging.debug('Audio Device:\n'
                      '{pactl}'
                      .format(pactl=Command('pactl stat | grep Sink').run().stdout))

        # Check kernel logs using firmware test suite
        command = Command('fwts -r stdout klog dmesg_common oops').run()
        if command.process.returncode != 0:
            #raise TestFailed('Problem found in logs by fwts')
            logging.error('Problem found in logs by fwts')


class MessageDialog(object):
    """
    Simple wrapper aroung gtk.MessageDialog
    """
    def __init__(self, title, message, type=gtk.MESSAGE_INFO):
        self.title = title
        self.message = message
        self.type = type

    def run(self):
        dialog = gtk.MessageDialog(buttons=gtk.BUTTONS_OK,
                                   message_format=self.message,
                                   type=self.type)
        logging.info(self.message)
        dialog.set_title(self.title)
        dialog.run()
        dialog.destroy()


class AutoLoginConfigurator(object):
    """
    Enable/disable autologin configuration
    to make sure that reboot test will work properly
    """
    CONFIG_FILENAME = '/etc/lightdm/lightdm.conf'
    TEMPLATE = """
[SeatDefaults]
greeter-session=unity-greeter
user-session=ubuntu
autologin-user={username}
autologin-user-timeout=0
"""

    def enable(self):
        """
        Make sure user will autologin in next reboot
        """
        logging.debug('Enabling autologin for this user...')
        if os.path.exists(self.CONFIG_FILENAME):
            for backup_filename in self.generate_backup_filename():
                if not os.path.exists(backup_filename):
                    shutil.copyfile(self.CONFIG_FILENAME, backup_filename)
                    shutil.copystat(self.CONFIG_FILENAME, backup_filename)
                    break

        with open(self.CONFIG_FILENAME, 'w') as f:
            f.write(self.TEMPLATE.format(username=os.getenv('SUDO_USER')))


    def disable(self):
        """
        Remove latest configuration file
        and use the same configuration that was in place
        before running the test
        """
        logging.debug('Restoring autologin configuration...')
        backup_filename = None
        for filename in self.generate_backup_filename():
            if not os.path.exists(filename):
                break
            backup_filename = filename

        if backup_filename:
            shutil.copy(backup_filename, self.CONFIG_FILENAME)
            os.remove(backup_filename)
        else:
            os.remove(self.CONFIG_FILENAME)

    def generate_backup_filename(self):
        backup_filename = self.CONFIG_FILENAME + '.bak'
        yield backup_filename

        index=0
        while True:
            index += 1
            backup_filename = (self.CONFIG_FILENAME
                               + '.bak.{0}'.format(index))
            yield backup_filename


class SudoersConfigurator(object):
    """
    Enable/disable reboot test to be executed as root
    to make sure that reboot test works properly
    """
    MARK = '# Automatically added by pm.py'
    SUDOERS = '/etc/sudoers'

    def enable(self):
        """
        Make sure that user will be allowed to execute reboot test as root
        """
        logging.debug('Enabling user to execute test as root...')
        command = ("sed -i -e '$a{mark}\\n"
                   "{user} ALL=NOPASSWD: /usr/bin/python' "
                   "{filename}".format(mark=self.MARK,
                                       user=os.getenv('SUDO_USER'),
                                       script=os.path.realpath(__file__),
                                       filename=self.SUDOERS))

        Command(command, verbose=False).run()


    def disable(self):
        """
        Revert sudoers configuration changes
        """
        logging.debug('Restoring sudoers configuration...')
        command = (("sed -i -e '/{mark}/,+1d' "
                    "{filename}")
                   .format(mark=self.MARK,
                           filename=self.SUDOERS))
        Command(command, verbose=False).run()


class AutoStartFile(object):
    """
    Generate autostart file contents and write it to proper location
    """
    TEMPLATE = """
[Desktop Entry]
Name={pm_operation} test
Comment=Verify {pm_operation} works properly
Exec=sudo /usr/bin/python {script} -r {repetitions} -w {wakeup} --hardware-delay {hardware_delay} --pm-delay {pm_delay} --min-pm-time {min_pm_time} --max-pm-time {max_pm_time} --append --total {total} --start {start} --pm-timestamp {pm_timestamp} {silent} --log-level={log_level} {pm_operation}
Type=Application
X-GNOME-Autostart-enabled=true
Hidden=false
"""
    def __init__(self, args):
        self.args = args

        # Generate desktop filename
        # based on environment variables
        username = os.getenv('SUDO_USER')
        default_config_directory = os.path.expanduser('~{0}/.config'
                                                      .format(username))
        config_directory = os.getenv('XDG_CONFIG_HOME',
                                     default_config_directory)
        autostart_directory = os.path.join(config_directory, 'autostart')
        if not os.path.exists(autostart_directory):
            os.makedirs(autostart_directory)

        basename = '{0}.desktop'.format(os.path.basename(__file__))
        self.desktop_filename = os.path.join(autostart_directory,
                                             basename)

    def write(self):
        """
        Write autostart file to execute the script on startup
        """
        logging.debug('Writing desktop file ({0!r})...'
                      .format(self.desktop_filename))

        contents = (self.TEMPLATE
                    .format(script=os.path.realpath(__file__),
                            repetitions=self.args.repetitions-1,
                            wakeup=self.args.wakeup,
                            hardware_delay=self.args.hardware_delay,
                            pm_delay=self.args.pm_delay,
                            min_pm_time=self.args.min_pm_time,
                            max_pm_time=self.args.max_pm_time,
                            total=self.args.total,
                            start=self.args.start,
                            pm_timestamp=int(time()),
                            silent='--silent' if self.args.silent else '',
                            log_level=self.args.log_level_str,
                            pm_operation=self.args.pm_operation))
        logging.debug(contents)

        with open(self.desktop_filename, 'w') as f:
            f.write(contents)

    def remove(self):
        """
        Remove autostart file to avoid executing the script on startup
        """
        if os.path.exists(self.desktop_filename):
            logging.debug('Removing desktop file ({0!r})...'
                          .format(self.desktop_filename))
            os.remove(self.desktop_filename)


class LoggingConfiguration(object):
    @classmethod
    def set(cls, log_level, log_filename, append):
        """
        Configure a rotating file logger
        """
        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)

        # Log to sys.stderr using log level passed through command line
        if log_level != logging.NOTSET:
            log_handler = logging.StreamHandler()
            formatter = logging.Formatter('%(levelname)-8s %(message)s')
            log_handler.setFormatter(formatter)
            log_handler.setLevel(log_level)
            logger.addHandler(log_handler)

        # Log to rotating file using DEBUG log level
        log_handler = logging.handlers.RotatingFileHandler(log_filename, mode='a+',
                                                           backupCount=3)
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
        log_handler.setFormatter(formatter)
        log_handler.setLevel(logging.DEBUG)
        logger.addHandler(log_handler)

        if not append:
            # Create a new log file on every new
            # (i.e. not scheduled) invocation
            log_handler.doRollover()


class MyArgumentParser(object):
    """
    Command-line argument parser
    """
    def __init__(self):
        """
        Create parser object
        """
        pm_operations = ('suspend', 'hibernate', 'poweroff', 'reboot')
        description = 'Run power management operation as many times as needed'
        epilog = ('Unknown arguments will be passed to the underlying command: '
                  'pm-suspend, pm-hibernate, poweroff or reboot.')
        parser = ArgumentParser(description=description, epilog=epilog)
        parser.add_argument('-r', '--repetitions', type=int, default=1,
                            help=('Number of times that the power management operation '
                                  'has to be repeated (%(default)s by default)'))
        parser.add_argument('-w', '--wakeup', type=int, default=60,
                            help=('Timeout in seconds for the wakeup alarm '
                                  '(%(default)s by default). '
                                  "Note: wakeup alarm won't be scheduled for reboot."))
        parser.add_argument('--min-pm-time', dest='min_pm_time',
                            type=int, default=0,
                            help=('Minimum time in seconds that it should take '
                                  'the power management operation each cycle '
                                  '(0 for reboot and wakeup time minus two seconds '
                                  'for the other power management operations by default)'))
        parser.add_argument('--max-pm-time', dest='max_pm_time',
                            type=int, default=300,
                            help=('Maximum time in seconds that it should take '
                                  'the power management operation each cycle '
                                  '(%(default)s by default)'))
        parser.add_argument('--pm-delay', dest='pm_delay',
                            type=int, default=5,
                            help=('Delay in seconds after hardware information '
                                  'has been gathered and before executing '
                                  'the power management operation '
                                  '(%(default)s by default)'))
        parser.add_argument('--hardware-delay', dest='hardware_delay',
                            type=int, default=30,
                            help=('Delay in seconds before gathering hardware '
                                  'information (%(default)s by default)'))
        parser.add_argument('--silent', action='store_true',
                            help=("Don't display any dialog when test is complete "
                                  'to let the script be used in automated tests'))
        log_levels = ['notset', 'debug', 'info', 'warning', 'error', 'critical']
        parser.add_argument('--log-level', dest='log_level_str', default='info',
                            choices=log_levels,
                            help=('Log level. '
                                  'One of {0} or {1} (%(default)s by default)'
                                  .format(', '.join(log_levels[:-1]), log_levels[-1])))
        parser.add_argument('pm_operation', choices=pm_operations,
                            help=('Power management operation to be performed '
                                  '(one of {0} or {1!r})'
                                  .format(', '.join(map(repr, pm_operations[:-1])),
                                          pm_operations[-1])))

        # Test timestamps
        parser.add_argument('--start', type=int, default=0, help=SUPPRESS)
        parser.add_argument('--pm-timestamp', dest='pm_timestamp',
                            type=int, default=0, help=SUPPRESS)

        # Append to log on subsequent startups
        parser.add_argument('--append', action='store_true', default=False, help=SUPPRESS)

        # Total number of iterations initially passed through the command line
        parser.add_argument('--total', type=int, default=0, help=SUPPRESS)
        self.parser = parser

    def parse(self):
        """
        Parse command-line arguments
        """
        args, extra_args = self.parser.parse_known_args()
        args.log_level = getattr(logging, args.log_level_str.upper())

        # Total number of repetitions
        # is the number of repetitions passed through the command line
        # the first time the script is executed
        if not args.total:
            args.total = args.repetitions

        # Test start time automatically set on first iteration
        if not args.start:
            args.start = int(time())

        # Wakeup time set to 0 for 'reboot'
        # since wakeup alarm won't be scheduled
        if args.pm_operation == 'reboot':
            args.wakeup = 0
            args.min_pm_time = 0

        # Minimum time for each power management operation
        # is set to the wakeup time
        if not args.min_pm_time:
            min_pm_time = args.wakeup - 2
            if min_pm_time < 0:
                min_pm_time = 0
            args.min_pm_time = min_pm_time

        # Log filename shows clearly the type of test (pm_operation)
        # and the times it was repeated (repetitions)
        args.log_filename = os.path.join('/var/log',
                                         ('{0}.{1}.{2}.log'
                                          .format(os.path.basename(__file__),
                                                  args.pm_operation,
                                                  args.total)))
        return args, extra_args


if __name__ == '__main__':
    sys.exit(main())
