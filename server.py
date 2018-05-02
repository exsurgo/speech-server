"""An HTTP/Websocket server for transcribing audio requests.

The server allows a client to connect via a websocket connection,
receives an incoming audio byte stream, and returns transcribed
text from the Cloud Speech API.

It uses 443 (PORT) for production since HTTPS/WSS is used. Port 1443
(PORT + 1000) is used for local development since <1000 ports are blocked in
many environments. The environment variable 'IS_DEVELOPMENT' should be set
when running locally. This turns on logging, opens PORT + 1000, and ensures
the use of a localhost SSL certificate.

It also exposes a simple HTTPS server for serving the client and providing status.

Routes:
  WebSocket (Audio Data): http://{host}/ws
  Client App:   http://{host}/client
  Status: http://{host}/status
"""

from __future__ import absolute_import

import itertools
import json
import logging
import os
import sys
import time
import wave
import transcriber
from tornado import ioloop
from tornado import web
from tornado import websocket


# Map of currently connected clients.
_clients = {}
_id_counter = itertools.count()

# Timeout in which connection is automatically closed
TIMEOUT = 60

# Port opened for HTTPS/WSS requests
PORT = 443

# Path to test HTML client file
CLIENT_PATH = 'client.html'


class SpeechHandler(websocket.WebSocketHandler):
  """A streaming speech handler for a websocket client.

  This handler receives incoming audio data from a websocket client such
  as an HTML5 browser, opens a connection with the Cloud Speech API, relays
  audio data, and returns any transcribed text to the client.

  Typical request example:
    Optionally, an initial JSON object can be sent to configure the service:
    {
      "rate": 16000,
      "encoding": 'LINEAR16',
      "language": 'en-US',
      "punctuation": true
    }

    Audio data should be relayed in real-time, as Int16 arrays.
    See "tests/test.html" for a sample client implementation.

  Attributes:
    transcriber: An instance of Transcriber class which is responsible for
        connecting to Speech API, and transcribing audio data.
  """

  def __init__(self, application, request, **kwargs):
    super(SpeechHandler, self).__init__(application, request, **kwargs)
    logging.info('Client Created')

    # Create a new Transcriber for relaying audio bytes to Speech API.
    self.transcriber = transcriber.Transcriber(
        on_transcribed=self.on_transcribed,
        on_transcribing=self.on_transcribing)

    # Create a unique id for the handler
    self._id = _id_counter.next()

    # Used for checking the timeout
    self._start_time = None

    # Used for for the _log_stream method
    self._log_columns = 0

    # Debug: Record audio files locally
    self._recorded_audio_data = []

  @property
  def id(self):
    """Returns a unique id for the client."""
    return self._id

  def open(self):
    """Saves client id after websocket is opened."""
    logging.info('Client Connected')
    if self.id not in _clients:
      _clients[self.id] = self

  def on_message(self, data):
    """Handles an incoming websocket message from the client.

    Args:
      data: This is either 1) a JSON string that defines the configuration
          (rate, language, encoding, punctuation), or 2) a chunk of raw
          audio data.
    """

    # Check for timeout.
    if self._start_time and self.transcriber.is_started:
      now = time.time()
      if (now - self._start_time).total_seconds() > TIMEOUT:
        logging.info('Timeout... Closing socket')
        self.close()
        return

    # Config, set by unicode JSON string, not a byte string.
    if isinstance(data, unicode):
      data = json.loads(data)
      for name in ['rate', 'language', 'encoding', 'punctuation']:
        if name in data and data[name] is not None:
          setattr(self.transcriber, name, data[name])
      logging.info('Connecting to API...')
      logging.info('Sample Rate: %d', self.transcriber.rate)
      logging.info('Encoding: %s', self.transcriber.encoding)
      logging.info('Language: %s', self.transcriber.language)
      logging.info('Punctuation: %s', self.transcriber.punctuation)
      self.transcriber.start()
      return

    # Ensure is started.
    if not self.transcriber.is_started:
      self._start_time = time.time()
      self.transcriber.start()

    # Transcribe binary data.
    self._log_stream()
    self.transcriber.transcribe(data)

  def on_api_connected(self):
    """Logs the connection with Speech API."""
    logging.info('Connected to Speech API')
    logging.info('Incoming WebSocket Stream:  I')
    logging.info('Outgoing API Stream:  O')

  def on_transcribing(self, recorded_audio_data):
    """Logs an outgoing data stream to the Speech API.

    Args:
      recorded_audio_data: A chunk of binary audio data in LINEAR16 format.
    """
    self._log_stream(True)

    # Debug: Record audio files
    if self.request.host == 'localhost:%i' % get_port():
      self._recorded_audio_data.append(recorded_audio_data)

  def on_transcribed(self, result):
    """Returns the transcribed result from the Cloud Speech API.

    The result is logged, and send to the client as serialized JSON.

    Args:
      result: A named tuple with fields 'text' and 'is_final'.  text is the
          transcribed text or a best guess, and is_final indicates that the
          phrase utterance is complete.
    """
    self._log_columns = 0
    if is_dev():
      sys.stdout.write('\n')
      logging.info('Transcribed Text: %s', result.text)
    if result and result.text:
      self.write_message(
          json.dumps({
              'text': result.text,
              'isFinal': result.is_final
          }))

  def on_close(self):
    """Removes client id when websocket connection is closed."""
    logging.info('Client Disconnected')
    self.transcriber.stop()
    if self.id in _clients:
      del _clients[self.id]

    # Record audio if running locally
    if self.request.host == 'localhost:%i' % get_port():
      self._record_audio_files()

  def check_origin(self, _):
    """Allows cross-origin websocket requests."""
    return True

  def _log_stream(self, output=False):
    """Displays the audio input/output stream to the console.

    This provides a simple way to visualize the input/output streams, and to
    ensure the streams are roughly in sync.

    Args:
      output: An optional boolean to indicate output "O" rather than input "I".
    """
    if is_dev():
      if self._log_columns > 80:
        self._log_columns = 0
        sys.stdout.write('\n')
      sys.stdout.write('O' if output else 'I')
      self._log_columns += 1

  def _record_audio_files(self):
    """Debug: Records a raw data file and a wav file."""
    binary_data = ''.join(self._recorded_audio_data)
    if not os.path.exists('recordings'):
      os.makedirs('recordings')
    path = 'recordings/audio-{}'.format(self.id)
    wav = wave.open(path + '.wav', 'wb')
    wav.setparams((1, 2, self.transcriber.rate, 0, 'NONE', 'not compressed'))
    wav.writeframes(binary_data)
    wav.close()
    raw = open(path + '.raw', 'wb')
    raw.write(binary_data)
    raw.close()


class CORSHandler(web.RequestHandler):
  """A simple handler base class for enabling CORS."""

  def options(self):
    """Returns 204 no content with CORS headers."""
    self.set_status(204)
    self.finish()

  def set_default_headers(self):
    """Sets headers for CORS."""
    self.set_header('Access-Control-Allow-Origin', '*')
    self.set_header('Access-Control-Allow-Headers', 'x-requested-with')
    self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')


class StatusHandler(CORSHandler):
  """A handler for returning on 'OK' 200 status, used for health checks."""

  def get(self):
    """Handles an HTTP GET request for 'https://{host}/status'."""
    self.write('OK')
    self.set_header('Content-Type', 'text/plain; charset=utf-8')


class ClientHandler(CORSHandler):
  """Handler for returning a simple test client."""

  def get(self):
    """Handles an HTTP GET request for 'https://{host}/client'."""
    self.render(CLIENT_PATH)


def get_port():
  """Returns the HTTP port that should be used.

  See file level comment for additional details.

  Returns:
    An integer representing the port number.
  """
  if is_dev():
    return 1000 + PORT
  else:
    return PORT


def is_dev():
  """Returns true if run in a local development environment.

  See file level comment for additional details.

  Returns:
    A boolean indicating is development.
  """
  return 'IS_DEVELOPMENT' in os.environ


if __name__ == '__main__':

  # Logging
  if is_dev():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    logging.getLogger('requests').setLevel(logging.WARNING)

  # Setup routes and create app
  app = web.Application([
      (r'/ws', SpeechHandler),
      (r'/status', StatusHandler),
      (r'/client', ClientHandler),
  ])

  # HTTPS/WSS handler
  cert_name = ('localhost' if is_dev() else 'prod')
  app.listen(
      get_port(),
      ssl_options={
          'certfile': '.private/%s.crt' % cert_name,
          'keyfile': '.private/%s.key' % cert_name,
      })
  logging.info('Secure libs listening on port %s', get_port())

  # Start libs loop
  ioloop.IOLoop.instance().start()
