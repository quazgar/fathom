from contextlib import contextmanager
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from importlib.resources import open_binary
import os
from os import devnull
from os.path import join
import pathlib
import platform
from shutil import copyfile, move
import signal
from subprocess import CalledProcessError, run
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from zipfile import ZipFile, ZIP_DEFLATED

from click import argument, ClickException, command, option, Path, progressbar
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException
from selenium.webdriver.support.ui import Select

from .list import samples_from_dir


class GracefulError(ClickException):
    """An error that allows for a graceful shutdown"""


class UngracefulError(ClickException):
    """An error that does not allow for a graceful shutdown"""


class Timeout(Exception):
    """``retry()`` finished all its tries without succeeding."""


class SilentRequestHandler(SimpleHTTPRequestHandler):
    """A request handler that will not output the log for each request to the
    terminal

    Not only is this distracting but it also seems to prevent requests from
    being served when using the ThreadingHTTPServer.

    """
    def log_message(self, format, *args):
        pass


@command()
@argument('ruleset_file', type=Path(exists=True, dir_okay=False))
@argument('trainee_id', type=str)
@argument('samples_directory', type=Path(exists=True, file_okay=False))
@argument('output_file', type=Path(exists=False, dir_okay=False))
@option('--show-browser', '-s',
        default=False,
        is_flag=True,
        help='Show browser window while running. Browser is run in headless mode by default.')
def main(ruleset_file, trainee_id, samples_directory, output_file, show_browser):
    """Create feature vectors for a directory of training samples using a
    Fathom ruleset.

    \b
    RULESET_FILE: Path to the rulesets.js file. The file must be pre-bundled, if
        necessary (containing no import statements).
    TRAINEE_ID: The ID of the Fathom trainee in rulesets.js to create vectors
        for
    SAMPLES_DIRECTORY: Path to the directory containing the sample pages
    OUTPUT_FILE: Where to save the resulting vector file

    \b
    This tool will run an instance of Firefox to use the Vectorizer within the
    FathomFox adddon. Required for this tool to work are...
      * node (and npm, which ships with it)
      * Firefox

    Please note that this utility is considered experimental due to the use of
    os.kill() when shutting down during vectorization. We are working on fixing
    this. Repeatedly stopping this program during vectorization may cause
    problems with other currently running Firefox processes.

    """
    output_file = pathlib.Path(output_file)
    sample_filenames = [str(sample.relative_to(samples_directory))
                        for sample in samples_from_dir(samples_directory)]
    with TemporaryDirectory() as download_dir:  # TODO: Have the individual functions make their own temp dirs.
        download_dir = pathlib.Path(download_dir)
        with fathom_fox_addon(ruleset_file) as addon_and_geckodriver:
            addon_path, geckodriver_path = addon_and_geckodriver
            with serving(samples_directory):
                with running_firefox(addon_path,
                                     download_dir,
                                     show_browser,
                                     geckodriver_path) as firefox:
                    vectorize(firefox, trainee_id, sample_filenames, output_file)


@contextmanager
def fathom_fox_addon(ruleset_file):
    """Return a Path to a FathomFox extension containing your ruleset and
    another to the geckodriver executable."""
    print('Building FathomFox with your ruleset...', end='', flush=True)
    with TemporaryDirectory() as temp:
        temp_dir = pathlib.Path(temp)

        # Extract a fresh copy of FathomFox and Fathom (which it needs to build
        # your ruleset) from resources stored in this Python package. By
        # including it directly from the source tree, we save downloading it
        # afterward (and figuring out where to put it), and FathomFox can
        # continue to refer to Fathom as file:../fathom for easy development.
        with open_binary('fathom_web', 'fathom.zip') as fathom_zip:
            zip = ZipFile(fathom_zip)
            zip.extractall(temp_dir)

        # Copy in your ruleset:
        fathom_fox = temp_dir / 'fathom_fox'
        copyfile(ruleset_file, fathom_fox / 'src' / 'rulesets.js')

        def run_in_fathom_fox(*args):
            """Run a command using the FathomFox dir as the working dir."""
            return run(args, cwd=fathom_fox, capture_output=True, check=True)

        # Install yarn, because it installs FathomFox's dependencies in 15s
        # rather than 30s. And once installed once, yarn itself takes only 1.5s
        # to install again. Also, it has security advantages. Though this
        # install itself isn't hashed, so we're just trusting NPM.
        run_in_fathom_fox('npm', 'install', 'yarn@1.22.4')
        # TODO: Better error message for not having node

        # Figure out how to invoke yarn:
        if platform.system() == 'Windows':
            # Running yarn through the Command Prompt will cause a cancellation
            # prompt to appear if the user presses ctrl+c during yarn's
            # execution. We do not want this. We want this program to stop
            # immediately when a user hits ctrl+c. The work around is to
            # execute yarn through node using yarn.js. To find this file, we use
            # `which`. See: https://stackoverflow.com/questions/39085380/how-
            # can-i-suppress-terminate-batch-job-y-n-confirmation-in-powershell
            # TODO: Better error message for not having which
            # TODO: Do we need to call `which` on plain, Cygwin-less Windows? We
            #       don't on the Mac.
            yarn_dir = run_in_fathom_fox('which', 'yarn').stdout.decode().strip()[:-4]
            if sys.platform == 'cygwin':
                # Under cygwin, `where` returns a cygwin path, so we need to
                # transform this into a proper Windows path:
                yarn_dir = run_in_fathom_fox('cygpath', '-w', yarn_dir).stdout.decode().strip()
            yarn_cmd = ['node', join(yarn_dir, 'yarn.js')]
        else:
            yarn_cmd = ['yarn']

        # Pull in npm dependencies:
        run_in_fathom_fox(*yarn_cmd, 'install')
        # Should we cache the yarn-installed dir to save 15 seconds? We can
        # hash fathom.zip and leave a turd in the cache to validate. But stay
        # re-entrant; you might want to run 2 vectorizations at once.

        # Build FathomFox:
        run_in_fathom_fox(fathom_fox / 'node_modules' / '.bin' / 'rollup', '-c')

        # Smoosh FathomFox down into an XPI. The Firefox webdriver requires we
        # load custom addons using .xpi files.
        addon_path = temp_dir / 'fathom-fox.xpi'
        fathom_fox = zip_dir(fathom_fox / 'addon', addon_path)

        print('done.')
        yield addon_path, (temp_dir / 'fathom_fox' / 'node_modules' / '.bin' / 'geckodriver')


def zip_dir(dir, archive_path):
    """Compress an entire dir, and write it to ``archive_path``."""
    with ZipFile(archive_path, 'w', compression=ZIP_DEFLATED) as archive:
        for file in dir.rglob('*'):
            archive.write(file, file.relative_to(dir))


@contextmanager
def serving(samples_directory):
    """Create a local HTTP server for the samples."""
    print('Starting HTTP file server...', end='', flush=True)
    RequestHandler = partial(SilentRequestHandler, directory=samples_directory)
    server = ThreadingHTTPServer(('localhost', 8000), RequestHandler)
    Thread(target=server.serve_forever).start()
    print('done.')
    yield
    server.shutdown()
    server.server_close()  # joins threads in ThreadingHTTPServer


@contextmanager
def running_firefox(fathom_fox, download_dir, show_browser, geckodriver_path):
    """Configure and return a running Firefox to run the vectorizer with.

    Sets headless mode, sets the download directory to the desired output
    directory, turns off page caching, and installs FathomFox.

    """
    print('Running Firefox...', end='', flush=True)
    options = webdriver.FirefoxOptions()
    options.headless = not show_browser

    profile = webdriver.FirefoxProfile()
    profile.set_preference('browser.download.folderList', 2)
    profile.set_preference('browser.download.dir', str(pathlib.Path(download_dir).absolute()))
    profile.set_preference('browser.cache.disk.enable', False)
    profile.set_preference('browser.cache.memory.enable', False)
    profile.set_preference('browser.cache.offline.enable', False)

    firefox = webdriver.Firefox(
        executable_path=str(geckodriver_path.absolute()),
        options=options,
        firefox_profile=profile,
        service_log_path=devnull,
    )

    firefox.install_addon(str(fathom_fox), temporary=True)
    firefox_pid = firefox.capabilities['moz:processID']
    geckodriver_pid = firefox.service.process.pid
    print('done.')

    # There is a graceful shutdown path and an ungraceful shutdown path. If the
    # vectorizer has started running, we use the ungraceful shutdown path. This
    # is because Firefox becomes unresponsive to the call to `quit()`.
    graceful_shutdown = False
    try:
        yield firefox
        graceful_shutdown = True
    except GracefulError:
        graceful_shutdown = True
        raise  # happens AFTER the finally
    finally:
        if graceful_shutdown:
            firefox.quit()
        else:
            # This is the only way I could get killing the program with ctrl+c to work properly.
            signal_for_killing = signal.CTRL_C_EVENT if os.name == 'nt' else signal.SIGTERM
            try:
                os.kill(firefox_pid, signal_for_killing)
            except (SystemError, ProcessLookupError):
                pass
            os.kill(geckodriver_pid, signal_for_killing)


def vectorize(firefox, trainee_id, sample_filenames, output_path):
    """Set up the vectorizer and run it, creating the vectors file.

    Navigate to the vectorizer page of FathomFox, paste the sample filenames
    into the text area and hit the vectorize button.

    We monitor the status text area for errors and to see how many samples
    have been vectorized, so we know when the vectorizer has stopped running.

    """
    print('Configuring Vectorizer...', end='', flush=True)
    # Navigate to the vectorizer page
    fathom_fox_uuid = get_fathom_fox_uuid(firefox)
    firefox.get(f'moz-extension://{fathom_fox_uuid}/pages/vector.html')

    # TODO: Before selecting, check if the page is reporting an error about no ruleset present. If there is, raise a GracefulError.
    ruleset_dropdown_selector = Select(firefox.find_element_by_id('ruleset'))
    ruleset_dropdown_selector.select_by_visible_text(trainee_id)

    pages_text_area = firefox.find_element_by_id('pages')
    pages_text_area.send_keys('\n'.join(sample_filenames))

    number_of_samples = len(sample_filenames)
    status_box = firefox.find_element_by_id('status')
    vectorize_button = firefox.find_element_by_id('freeze')
    completed_samples = 0
    print('done.')

    with progressbar(length=number_of_samples, label='Running Vectorizer...') as bar:
        vectorize_button.click()
        while completed_samples < number_of_samples:
            try:
                status_box_text = status_box.text
            except NoSuchWindowException:
                raise UngracefulError('Vectorization aborted: Firefox window closed during vectorization')
            try:
                failure_detected = 'failed' in status_box_text
            except TypeError:
                raise UngracefulError('Vectorization aborted: Firefox window closed during vectorization')
            if failure_detected:
                error = extract_error_from(status_box_text)
                raise UngracefulError(f'Vectorization failed with error:\n{error}')
            now_completed_samples = len([line for line in status_box_text.splitlines() if line.endswith(': vectorized')])
            bar.update(now_completed_samples - completed_samples)
            completed_samples = now_completed_samples
            sleep(.25)

    download_dir = pathlib.Path(firefox.profile.default_preferences['browser.download.dir'])
    new_file = wait_for_vectors_in(download_dir)
    move(str(new_file.absolute()), str(output_path.absolute()))  # won't overwrite an existing file
    print(f'Vectors saved to {str(output_path)}')


def get_fathom_fox_uuid(firefox):
    """Try to get the internal UUID for FathomFox from `prefs.js`.

    We use a loop to try multiple times because the `prefs.js` file needs a
    little time to update before the fathom addon information appears. Five
    seconds seems adequate since one second has always worked for me (Daniel).

    """
    def get_uuid():
        prefs = (pathlib.Path(firefox.capabilities.get('moz:profile')) / 'prefs.js').read_text().split(';')
        uuids = next((line for line in prefs if 'extensions.webextensions.uuids' in line)).split(',')
        fathom_fox_uuid = next((line for line in uuids if '{954efd86-8f62-49e7-8a65-80016051e382}' in line)).split('\\"')[3]
        return fathom_fox_uuid

    try:
        return retry(get_uuid)
    except Timeout:
        raise GracefulError('Could not find UUID for FathomFox. No entry in the prefs.js file.')


def extract_error_from(status_text):
    """Look for errors in the vectorizer's status text.

    If there is an error, raise an exception, causing an ungraceful shutdown.

    """
    lines = status_text.splitlines()
    for line in lines:
        if 'failed:' in line:
            return line
    raise UngracefulError(f'There was a vectorizer error, but we could not find it in {status_text}')


def wait_for_vectors_in(download_dir):
    """Wait for a vector file to appear in the downloads directory, and return
    its Path.

    The file system needs a little little time to update before the file
    appears. Five seconds seems adequate since one second has always worked for
    me (Daniel).

    """
    try:
        return retry(lambda: next(download_dir.glob('vectors*.json')))
    except Timeout:
        contents = ', '.join(item.name for item in download_dir.iterdir())
        raise GracefulError(f'Could not find vectors*.json in downloads folder. Present items were: {contents}.')


def retry(function, max_tries=5):
    """Try to execute a function some number of times before raising Timeout.

    :arg function: A function that likely has some time dependency and you want
        to try executing it multiple times to wait for the time dependency to
        resolve
    :arg max_tries: The number of times to try the function before raising
        Timeout

    """
    for _ in range(max_tries):
        try:
            return function()
        except Exception:  # noqa: E722
            sleep(1)
    else:
        raise Timeout()
