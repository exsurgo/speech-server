"""Transcribes an audio byte stream to text via Cloud Speech API.

This library handles authentication, establishes an SSL connection, sends
audio data to the Speech API, handles any transcribed text, and enforces a
timeout which ensures the connection and thread are stopped.  To authenticate,
add default credentials environment variable, or add a credentials file
located at ".private/credentials.json".

  Typical usage example:

  def on_transcribed_callback(data):
    print(data)

  transcriber = Transcriber(
      rate=16000,
      punctuation=True,
      encoding='LINEAR16',
      language='en-US',
      interim_results=True,
      on_transcribed=on_transcribed_callback)

  transcriber.send(audio_data)
"""

from __future__ import absolute_import

import collections
import json
import os
import Queue
import threading

import gcloud.credentials
import grpc
from oauth2client import service_account

from google.cloud import speech_v1p1beta1 as cloud_speech
from google.cloud.speech_v1p1beta1 import enums
from google.cloud.speech_v1p1beta1 import types
from google.rpc import code_pb2

# Constants

# The Speech API has a streaming limit of 60 seconds of audio*, so keep the
# connection alive for that long, plus some more to give the API time to figure
# out the transcription.
# https://g.co/cloud/speech/limits#content
# Note: This is the suggest from docs and sample code
TIMEOUT_SECS = 8 * 60 * 60

SPEECH_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
API_HOST = 'speech.googleapis.com'
API_PORT = 443
CREDS_PATH = '.private/credentials.json'


# The result passed to the on_transcribed callback.
TranscribedResult = collections.namedtuple('TranscribedResult', 'text is_final')


class Transcriber(threading.Thread):
  """This class gets oauth credentials, connects to the Cloud Speech API.

  It connects via an SSL socket, relays an audio stream to the API, and
  receives the transcribed text results.  It subclasses Thread so that it
  can seamlessly interact with steams and single-threaded web servers such
  as Tornado.

  Attributes:
    encoding: The audio encoding type.
        https://cloud.google.com/speech/docs/encoding
    language: A string representing language as a BCP-47 language tag.
    rate: An int representing sample rate in Hertz of the audio sent.
    punctuation: A boolean to enable automatic punctuation.
    interim_results: A boolean indicating that results may be sent as they
        become available, even is the transcription is not complete.
    on_connected: An optional callback ran after a channel is connected
        and authorized.  No arguments are passed to callback.
    on_transcribing: An optional callback ran after audio data is sent to
        the Speech API.   The raw audio data is passed to the callback.
    on_transcribed: An optional callback ran after text is returned from
        the Speech API.  A TranscribedResult object with the properties
        text and is_final is passed to the callback.
    audio_stream: An optional audio stream object, primarily used for testing.
    is_connected: A boolean indicating that the connection is open.
    is_started: A boolean indicating that audio is being transcribed.
    timeout_secs: The number of seconds before he connection is closed.
  """

  def __init__(
      self,
      punctuation=True,
      encoding=enums.RecognitionConfig.AudioEncoding.LINEAR16,
      language='en-US',
      rate=44100,  # Default for Chrome.
      timeout_secs=TIMEOUT_SECS,
      interim_results=True,
      on_connected=None,
      on_transcribing=None,
      on_transcribed=None,
      audio_stream=None):
    """Initializes the default attributes for the class."""

    super(Transcriber, self).__init__()

    # Represents a stream of audio bytes.
    # This can be mocked for testing
    if audio_stream is None:
      self.audio_stream = QueuedAudioStream()
    else:
      self.audio_stream = audio_stream

    # Config
    self.encoding = encoding
    self.language = language
    self.punctuation = punctuation
    self.rate = rate
    self.timeout_secs = timeout_secs
    self.interim_results = interim_results

    # Callbacks
    self.is_connected = False
    self.on_connected = on_connected
    self.on_transcribing = on_transcribing
    self.on_transcribed = on_transcribed

    # Flags for determining if transcription is started.
    self.is_started = False

    # Prevents the thread from being started twice.  This is necessary in
    # certain async cases where timing issues are possible.
    self._thread_started = False

    # Stop the request stream once we're done with the loop,
    # otherwise it will continue in the thread forever.
    self._stop_event = threading.Event()

  def transcribe(self, data):
    """Sends a chunk of audio data to the Speech API for transcription."""
    self.audio_stream.write(data)

  def run(self):
    """Called from [start]. Connects to service and begins streaming."""

    # Exit if stop event occurred.
    if self._stop_event.is_set():
      return

    # Create SSL channel.
    channel = self._create_channel()
    self.is_started = True

    # Open stream
    service = cloud_speech.SpeechClient(channel)
    streaming_config = types.StreamingRecognitionConfig(
        config=types.RecognitionConfig(
            enable_automatic_punctuation=self.punctuation,
            encoding=self.encoding,
            sample_rate_hertz=self.rate,
            language_code=self.language,),
        interim_results=self.interim_results)

    try:
      request_stream = self._request_stream()
      resp_stream = service.streaming_recognize(
          streaming_config, request_stream)
      self._handle_results(resp_stream)
    finally:
      self.stop()

  def start(self):
    """Begins the transcription process in separate thread.

    The thread is started exactly once, and the connection is established.
    No error is thrown if start is called multiple times.
    """

    # Exit if already started.
    if self._thread_started:
      return
    else:
      self._thread_started = True

    # Start thread if stop event hasn't occurred.
    if not self._stop_event.is_set():
      super(Transcriber, self).start()

  def stop(self):
    """Stops audio streaming and thread."""
    self.is_started = False
    self._stop_event.set()

  def _handle_results(self, resp_stream):
    """Handles any resulting transcriptions returned from Speech API.

    Responses are provided for interim results as well. If the
    response is an interim one, then return isFinal=True in result.

    Args:
      resp_stream: A StreamingRecognizeRequest which is a generator that
          blocks until a response is provided by the Speech API.

    Raises:
      RuntimeError: An error occurred with the Speech API.
    """

    # Iterate when API result is returned.
    for resp in resp_stream:
      if resp.error.code != code_pb2.OK:
        self.stop()

        raise RuntimeError('Speech API Error: ' + resp.error.message)

      # Run on_transcribed callback with transcribed text.
      for result in resp.results:
        if self.on_transcribed:
          result = TranscribedResult(
              text=result.alternatives[0].transcript,
              is_final=result.is_final
          )
          self.on_transcribed(result)

  def _request_stream(self):
    """Starts the audio stream to the Speech API.

    The recognition config is passed to the API with the encoding, rate,
    language, punctuation, and interim_results values.  The stream passed is
    a generator that will block until a response is provided by the Speech API.

    Yields:
      google.cloud.speech.types.StreamingRecognizeRequest
    """

    # Run on_connected callback.
    self.is_connected = True
    if self.on_connected:
      self.on_connected()

    with self.audio_stream as stream:
      while not self._stop_event.is_set():
        data = stream.read()
        if data:
          # Run on_transcribing callback when sending to Speech API.
          if self.on_transcribing:
            self.on_transcribing(data)
          yield types.StreamingRecognizeRequest(audio_content=data)

  def _create_channel(self):
    """Opens an SSL channel to the Speech API.

    Returns:
      A grpc.Channel
    """

    # In order to make an https call, use an ssl channel with defaults.
    ssl_channel = grpc.ssl_channel_credentials()

    # Add a plugin to inject the creds into the header.
    auth_header = (
        'Authorization',
        'Bearer ' + _get_credentials().get_access_token().access_token)
    auth_plugin = grpc.metadata_call_credentials(
        lambda _, cb: cb([auth_header], None), name='google_creds')

    # Compose the 2 together for both ssl and google auth.
    composite_channel = grpc.composite_channel_credentials(ssl_channel,
                                                           auth_plugin)

    return grpc.secure_channel('{}:{}'.format(API_HOST, API_PORT),
                               composite_channel)


class QueuedAudioStream(object):
  """Represents incoming audio-data from a recorded source.

  This class acts as a buffer for a streaming audio source, such
  as incoming audio chunks recorded locally, and sent via websocket. This
  class is mockable to emulate incoming audio with a test file.
  """

  # We need to approximate incoming, real-time audio
  READ_WAIT_SECS = .010

  def __init__(self):
    # Used for storing incoming audio chunks.
    self.queue = Queue.Queue()

  def __enter__(self):
    return self

  def __exit__(self, *args):
    with self.queue.mutex:
      self.queue.queue.clear()

  def __call__(self, *args):
    return self

  def write(self, data):
    """Writes a chunk of audio data to Queue."""
    self.queue.put(data)

  def read(self):
    """Reads a chunk of audio bytes from the Queue."""
    if not self.queue.empty():
      data = self.queue.get(True, self.READ_WAIT_SECS)
      if data:
        return data


def _get_credentials():
  """Retrieves the credentials for oAuth authentication.

  First, the existence of a credentials.json file is checked. If it is not
  found, then the environment variable 'GOOGLE_APPLICATION_CREDENTIALS'
  is checked.

  Returns:
    oauth2client.service_account.ServiceAccountCredentials
  """

  # Try to get credentials from file if available.
  if os.path.isfile(CREDS_PATH):
    with open(CREDS_PATH, 'r') as cred_json:
      cred_data = json.loads(cred_json.read())
    creds = service_account.ServiceAccountCredentials
    creds = creds.from_json_keyfile_dict(cred_data)
    return creds.create_scoped([SPEECH_SCOPE])

  # Else try to get default credentials from environment.
  else:
    return gcloud.credentials.get_credentials().create_scoped([SPEECH_SCOPE])
