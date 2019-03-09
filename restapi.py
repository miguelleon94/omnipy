#!/usr/bin/python3
import base64
import os
from decimal import *

from Crypto.Cipher import AES
import simplejson as json
from flask import Flask, request, send_from_directory
from datetime import datetime
from podcomm.crc import crc8
from podcomm.packet import Packet
from podcomm.pdm import Pdm
from podcomm.pod import Pod
from podcomm.rileylink import RileyLink
from podcomm.definitions import *


app = Flask(__name__, static_url_path="/")
configureLogging()
logger = getLogger()


class RestApiException(Exception):
    def __init__(self, msg="Unknown"):
        self.error_message = msg

    def __str__(self):
        return self.error_message


def get_pod() -> Pod:
    try:
        return Pod.Load(POD_FILE + POD_FILE_SUFFIX, POD_FILE + POD_LOG_SUFFIX)
    except:
        logger.exception("Error while loading pod")
        return None


def get_pdm() -> Pdm:
    try:
        return Pdm(get_pod())
    except:
        logger.exception("Error while creating pdm instance")
        return None


def archive_pod():
    try:
        archive_suffix = datetime.utcnow().strftime("_%Y%m%d_%H%M%S")
        if os.path.isfile(POD_FILE + POD_FILE_SUFFIX):
            os.rename(POD_FILE + POD_FILE_SUFFIX, POD_FILE + archive_suffix + POD_FILE_SUFFIX)
        if os.path.isfile(POD_FILE + POD_LOG_SUFFIX):
            os.rename(POD_FILE + POD_LOG_SUFFIX, POD_FILE + archive_suffix + POD_LOG_SUFFIX)
    except:
        logger.exception("Error while archiving existing pod")


def create_response(success, response, pod_status=None):
    if pod_status is not None and pod_status.__class__ != dict:
        pod_status = pod_status.__dict__

    if response is not None and response.__class__ != dict:
        response = response.__dict__
    return json.dumps({"success": success, "response": response, "status": pod_status}, indent=4, sort_keys=True)


def verify_auth(request_obj):
    try:
        i = request_obj.args.get("i")
        a = request_obj.args.get("auth")
        if i is None or a is None:
            raise RestApiException("Authentication failed")

        iv = base64.b64decode(i)
        auth = base64.b64decode(a)

        with open(KEY_FILE, "rb") as keyfile:
            key = keyfile.read(32)

        cipher = AES.new(key, AES.MODE_CBC, iv)
        token = cipher.decrypt(auth)

        with open(TOKENS_FILE, "a+b") as tokens:
            tokens.seek(0, 0)
            found = False
            while True:
                read_token = tokens.read(16)
                if len(read_token) < 16:
                    break
                if read_token == token:
                    found = True
                    break

            if found:
                while True:
                    read_token = tokens.read(16)
                    if len(read_token) < 16:
                        tokens.seek(-16 - len(read_token), 1)
                        break
                    tokens.seek(-32, 1)
                    tokens.write(read_token)
                    tokens.seek(16, 1)
                tokens.truncate()

        if not found:
            raise RestApiException("Invalid authentication token")
    except RestApiException:
        logger.exception("Authentication error")
        raise
    except Exception:
        logger.exception("Error during verify_auth")
        raise


@app.route("/")
def main_page():
    try:
        return app.send_static_file("omnipy.html")
    except:
        logger.exception("Error while serving root file")


@app.route('/content/<path:path>')
def send_content(path):
    try:
        return send_from_directory("static", path)
    except:
        logger.exception("Error while serving static file from %s" % path)


def _api_result(result_lambda, generic_err_message):
    try:
        return create_response(True,
                               result=result_lambda())
    except RestApiException as rae:
        return create_response(False, rae)
    except Exception as e:
        logger.exception(generic_err_message)
        return create_response(False, e)


def get_api_version():
    return {"version_major": API_VERSION_MAJOR, "version_minor": API_VERSION_MINOR}


def create_token():
    with open(TOKENS_FILE, "a+b") as tokens:
        token = bytes(os.urandom(16))
        tokens.write(token)
    return {"token": base64.b64encode(token)}


def check_password():
    verify_auth(request)
    return None


def get_pdm_address():
    r = RileyLink()
    try:
        verify_auth(request)
        while True:
            timeout = 30000
            if request.args.get('timeout') is not None:
                timeout = int(request.args.get('timeout')) * 1000
                if timeout > 30000:
                    raise RestApiException("Timeout cannot be more than 30 seconds")

            data = r.get_packet(timeout)
            if data is None:
                p = None
                break

            if data is not None and len(data) > 2:
                calc = crc8(data[2:-1])
                if data[-1] == calc:
                    p = Packet.from_data(data[2:-1])
                    break
        if p is None:
            raise RestApiException("No pdm packet detected")

        return {"radio_address": p.address}
    finally:
        r.disconnect(ignore_errors=True)


def new_pod():
    verify_auth(request)

    pod = Pod()

    if request.args.get('id_lot') is not None:
        pod.id_lot = int(request.args.get('id_lot'))
    if request.args.get('id_t') is not None:
        pod.id_t = int(request.args.get('id_t'))
    if request.args.get('radio_address') is not None:
        pod.radio_address = int(request.args.get('radio_address'))

    archive_pod()
    pod.Save(POD_FILE + POD_FILE_SUFFIX)
    return None

def _int_parameter(obj, parameter):
    if request.args.get(parameter) is not None:
        obj.__dict__[parameter] = int(request.args.get(parameter))
        return True
    return False

def _float_parameter(obj, parameter):
    if request.args.get(parameter) is not None:
        obj.__dict__[parameter] = float(request.args.get(parameter))
        return True
    return False


def _bool_parameter(obj, parameter):
    if request.args.get(parameter) is not None:
        val = str(request.args.get(parameter))
        bval = False
        if val == "1" or val.capitalize() == "TRUE":
            bval = True
        obj.__dict__[parameter] = bval
        return True
    return False


def set_pod_parameters():
    verify_auth(request)

    pod = get_pod()
    reset_nonce = False
    if _int_parameter(pod, "id_lot"):
        reset_nonce = True
    if _int_parameter(pod, "id_t"):
        reset_nonce = True

    if reset_nonce:
        pod.nonce_last = None
        pod.nonce_seed = 0

    if _int_parameter(pod, "radio_address"):
        pod.radio_packet_sequence = 0
        pod.radio_message_sequence = 0

    _float_parameter(pod, "var_utc_offset")
    _float_parameter(pod, "var_maximum_bolus")
    _float_parameter(pod, "var_maximum_temp_basal_rate")
    _float_parameter(pod, "var_alert_low_reservoir")
    _int_parameter(pod, "var_alert_replace_pod")
    _bool_parameter(pod, "var_notify_bolus_start")
    _bool_parameter(pod, "var_notify_bolus_cancel")
    _bool_parameter(pod, "var_notify_temp_basal_set")
    _bool_parameter(pod, "var_notify_temp_basal_cancel")
    _bool_parameter(pod, "var_notify_basal_schedule_change")

    pod.Save()
    return None


def get_rl_info():
    verify_auth(request)
    r = RileyLink()
    return r.get_info()

def get_status():
    verify_auth(request)
    t = request.args.get('type')
    if t is not None:
        req_type = int(t)
    else:
        req_type = 0

    pdm = get_pdm()
    pdm.updatePodStatus(req_type)
    return pdm.pod

def deactivate_pod():
    verify_auth(request)
    pdm = get_pdm()
    pdm.deactivate_pod()
    archive_pod()
    return pdm.pod

def bolus():
    verify_auth(request)

    pdm = get_pdm()
    amount = Decimal(request.args.get('amount'))
    pdm.bolus(amount)
    return pdm.pod

def cancel_bolus():
    verify_auth(request)

    pdm = get_pdm()
    pdm.cancelBolus()
    return pdm.pod

def set_temp_basal():
    verify_auth(request)

    pdm = get_pdm()
    amount = Decimal(request.args.get('amount'))
    hours = Decimal(request.args.get('hours'))
    pdm.setTempBasal(amount, hours, False)
    return pdm.pod

def cancel_temp_basal():
    verify_auth(request)

    pdm = get_pdm()
    pdm.cancelTempBasal()
    return pdm.pod

def is_pdm_busy():
    pdm = get_pdm()
    return {"busy": pdm.is_busy()}

def acknowledge_alerts():
    verify_auth(request)

    mask = Decimal(request.args.get('alertmask'))
    pdm = get_pdm()
    pdm.acknowledge_alerts(mask)
    return pdm.pod

def shutdown():
    pdm = get_pdm()
    if pdm.is_busy():
        raise RestApiException("cannot shutdown while pdm is busy")
    else:
        return None

def restart():
    pdm = get_pdm()
    if pdm.is_busy():
        raise RestApiException("cannot restart while pdm is busy")
    else:
        return None

@app.route(REST_URL_GET_VERSION)
def a00():
    return _api_result(lambda: get_api_version(), "Failure while getting version")

@app.route(REST_URL_TOKEN)
def a01():
    return _api_result(lambda: create_token(), "Failure while creating token")

@app.route(REST_URL_CHECK_PASSWORD)
def a02():
    return _api_result(lambda: check_password(), "Failure while verifying password")

@app.route(REST_URL_GET_PDM_ADDRESS)
def a03():
    return _api_result(lambda: get_pdm_address(), "Failure while reading address from PDM")

@app.route(REST_URL_NEW_POD)
def a04():
    return _api_result(lambda: new_pod(), "Failure while creating a new pod")

@app.route(REST_URL_SET_POD_PARAMETERS)
def a05():
    return _api_result(lambda: set_pod_parameters(), "Failure while setting parameters")

@app.route(REST_URL_RL_INFO)
def a06():
    return _api_result(lambda: get_rl_info(), "Failure while getting RL info")

@app.route(REST_URL_STATUS)
def a07():
    return _api_result(lambda: get_status(), "Failure while executing getting pod status")

@app.route(REST_URL_ACK_ALERTS)
def a08():
    return _api_result(lambda: acknowledge_alerts(), "Failure while executing acknowledge alerts")

@app.route(REST_URL_DEACTIVATE_POD)
def a09():
    return _api_result(lambda: deactivate_pod(), "Failure while executing deactivate pod")

@app.route(REST_URL_BOLUS)
def a10():
    return _api_result(lambda: bolus(), "Failure while executing bolus")

@app.route(REST_URL_CANCEL_BOLUS)
def a11():
    return _api_result(lambda: cancel_bolus(), "Failure while executing cancel bolus")

@app.route(REST_URL_SET_TEMP_BASAL)
def a12():
    return _api_result(lambda: set_temp_basal(), "Failure while executing set temp basal")

@app.route(REST_URL_CANCEL_TEMP_BASAL)
def a13():
    return _api_result(lambda: cancel_temp_basal(), "Failure while executing cancel temp basal")

@app.route(REST_URL_PDM_BUSY)
def a14():
    return _api_result(lambda: is_pdm_busy(), "Failure while verifying if pdm is busy")

@app.route(REST_URL_OMNIPY_SHUTDOWN)
def a15():
    return _api_result(lambda: shutdown(), "Failure while executing shutdown")

@app.route(REST_URL_OMNIPY_RESTART)
def a16():
    return _api_result(lambda: restart(), "Failure while executing reboot")

if __name__ == '__main__':
    try:
        logger.info("Rest api is starting")
        if os.path.isfile(TOKENS_FILE):
            logger.debug("removing tokens from previous session")
            os.remove(TOKENS_FILE)
        if os.path.isfile(RESPONSE_FILE):
            logger.debug("removing response queue from previous session")
            os.remove(RESPONSE_FILE)
    except IOError as ioe:
        logger.warning("Error while removing stale files: %s", exc_info=ioe)

    try:
        os.system("sudo systemctl restart systemd-timesyncd")
        os.system("sudo systemctl daemon-reload")
    except:
        logger.exception("Error while reloading timesync daemon")

    try:
        app.run(host='0.0.0.0', port=4444)
    except:
        logger.exception("Error while running rest api, exiting")
        raise
