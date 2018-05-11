# Copyright (c) 2018 The Regents of the University of Michigan
# and the University of Pennsylvania
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Utility functions specifically for running jobs in MORF API.
"""

import boto3
import logging
import os
import subprocess
import tempfile
from morf.utils import *
from morf.utils.alerts import send_success_email, send_email_alert
from morf.utils.config import get_config_properties, combine_config_files, update_config_fields_in_section, MorfJobConfig
from morf.utils.log import set_logger_handlers
from urllib.parse import urlparse

module_logger = logging.getLogger('job_runner_utils.py')


def run_image(job_config, raw_data_bucket, course=None, session=None, level=None, label_type=None):
    """
    Run a docker image with the specified parameters.
    :param docker_url: URL for a built and compressed (.tar) docker image
    :param user_id: unique user id (string).
    :param job_id: unique job id (string).
    :param mode: mode to run image in; {extract, extract-holdout, train, test} (string).
    :param raw_data_bucket: raw data bucket; specify multiple buckets only if level == all.
    :param course: Coursera course slug or course shortname (string).
    :param session: 3-digit course session number (for trained model or extraction).
    :param level: level of aggregation of MORF API function; {session, course, all} (string).
    :param label_type: type of outcome label to use (required for model training and testing) (string).
    :return:
    """
    ## todo: define logger here
    docker_url = job_config.docker_url
    mode = job_config.mode
    s3 = job_config.initialize_s3()
    docker_exec = job_config.docker_exec
    # create local directory for processing on this instance
    with tempfile.TemporaryDirectory(dir=job_config.local_working_directory) as working_dir:
        try:
            fetch_file(s3, working_dir, docker_url, dest_filename="docker_image")
        except Exception as e:
            print("[ERROR] Error downloading file {} to {}".format(docker_url, working_dir))
        input_dir, output_dir = initialize_input_output_dirs(working_dir)
        # fetch any data or models needed
        if "extract" in mode:  # download raw data
            initialize_raw_course_data(job_config,
                                       raw_data_bucket=raw_data_bucket, mode=mode, course=course,
                                       session=session, level=level, input_dir=input_dir)
        # fetch training/testing data
        if mode in ["train", "test"]:
            initialize_train_test_data(job_config, raw_data_bucket=raw_data_bucket, level=level,
                                       label_type=label_type, course=course, session=session,
                                       input_dir=input_dir)
        if mode == "test":  # fetch models and untar
            download_models(job_config, course=course, session=session, dest_dir=input_dir, level=level)
        # load the docker image and get its key
        local_docker_file_location = "{}/docker_image".format(working_dir)
        cmd = "{} load -i {};".format(job_config.docker_exec, local_docker_file_location)
        logger.info("running: " + cmd)
        output = subprocess.run(cmd, stdout=subprocess.PIPE, shell = True)
        print(output.stdout.decode("utf-8"))
        load_output = output.stdout.decode("utf-8")
        if "sha256:" in load_output:
            image_uuid = output.stdout.decode("utf-8").split("sha256:")[-1].strip()
        else: #image is tagged
            image_uuid = load_output.split()[-1].strip()
        # build docker run command and execute the image
        if mode == "extract-holdout":  # call docker image with mode == extract
            cmd = "{} run --network=\"none\" --rm=true --volume={}:/input --volume={}:/output {} --course {} --session {} --mode {}".format(
                docker_exec, input_dir, output_dir, image_uuid, course, session, "extract")
        else:  # proceed as normal
            cmd = "{} run --network=\"none\" --rm=true --volume={}:/input --volume={}:/output {} --course {} --session {} --mode {}".format(
                docker_exec, input_dir, output_dir, image_uuid, course, session, mode)
        # add any additional client args to cmd
        if job_config.client_args:
            for argname, argval in job_config.client_args.items():
                cmd += " --{} {}".format(argname, argval)
        logger.info("running: " + cmd)
        subprocess.call(cmd, shell=True)
        # cleanup
        cmd = "{} rmi --force {}".format(docker_exec, image_uuid)
        logger.info("running: " + cmd)
        subprocess.call(cmd, shell=True)
        # archive and write output
        archive_file = make_output_archive_file(output_dir, job_config, course = course, session = session)
        move_results_to_destination(archive_file, job_config, course = course, session = session)
    return


def run_job(job_config, course, session, level, raw_data_bucket=None, label_type=None, raw_data_buckets=None):
    """
    Call job runner with correct parameters.
    :param job_config: MorfJobConfig object.
    :param course: course id (string); set as None if level == all.
    :param session: session number (string); set as none if level != session.
    :param level: one of {session, course, all}
    :param raw_data_bucket: name of bucket containing raw data.
    :param label_type: user-specified label type.
    :param raw_data_buckets: list of buckets (for use with level == all)
    :return: result of call to subprocess.call().
    """
    logger = job_config.getLogger(__name__)
    if not raw_data_buckets:
        raw_data_buckets = job_config.raw_data_buckets
    # todo: just set default values as none; no need for control flow below
    # todo: specify bucket here and make a required argument (currently run_image just defaults to using morf-michigan)
    # todo: different calls to run_image for each level are probably not necessary; all defaults are set to 'none'
    logger.info("running docker image {} user_id {} job_id {} course {} session {} mode {}"
          .format(job_config.docker_url, job_config.user_id, job_config.job_id, course, session, job_config.mode))
    if level == "all":
        run_image(job_config, raw_data_bucket=raw_data_buckets, level=level,
                  label_type=label_type)
    elif level == "course":
        run_image(job_config, raw_data_bucket, course=course, level=level, label_type=label_type)
    elif level == "session":
        run_image(job_config, raw_data_bucket, course=course, session=session, level=level, label_type=label_type)
    return None


def run_morf_job(job_config, email_to = None, no_cache = False):
    """
    Wrapper function to run complete MORF job.
    :param client_config_url: url to client.config file.
    :param server_config_url: url to server.config file.
    :return:
    """
    logger = set_logger_handlers(module_logger, job_config)
    logger.info("running job id: {}".format(job_config.morf_id))
    controller_script_name = "controller.py"
    docker_image_name = "docker_image"
    combined_config_filename = "config.properties"
    s3 = job_config.initialize_s3()
    # create temporary directory in local_working_directory from server.config
    with tempfile.TemporaryDirectory(dir=job_config.local_working_directory) as working_dir:
        os.chdir(working_dir)
        # from client.config, fetch and download the following: docker image, controller script
        try:
            fetch_file(s3, working_dir, job_config.docker_url, dest_filename=docker_image_name, job_config=job_config)
            fetch_file(s3, working_dir, job_config.controller_url, dest_filename=controller_script_name, job_config=job_config)
            if not no_cache: # cache job files in s3 unless no_cache parameter set to true
                cache_job_file_in_s3(job_config, filename = docker_image_name)
                cache_job_file_in_s3(job_config, filename = controller_script_name)
        except KeyError as e:
            cause = e.args[0]
            logger.error("[Error]: field {} missing from client.config file.".format(cause))
            sys.exit(-1)
        # change working directory and run controller script with notifications for initialization and completion
        job_config.update_status("INITIALIZED")
        send_email_alert(job_config)
        subprocess.call("python3 {}".format(controller_script_name), shell = True)
        job_config.update_status("SUCCESS")

        send_success_email(job_config)
        return
