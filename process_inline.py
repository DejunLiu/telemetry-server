#!/usr/bin/env python
# encoding: utf-8
"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import argparse
import time
import multiprocessing
from multiprocessing import Process, Queue
import Queue as Q
import simplejson as json
import imp
import sys
import os
import json
import marshal
import traceback
from datetime import date, datetime
from multiprocessing import Process
from telemetry_schema import TelemetrySchema
from persist import StorageLayout
import subprocess
from subprocess import Popen, PIPE
from boto.s3.connection import S3Connection
import util.timer as timer
import struct, gzip, StringIO
from convert import Converter, BadPayloadError
from revision_cache import RevisionCache
from persist import StorageLayout

def fetch_s3_files(files, fetch_cwd, bucket_name, aws_key, aws_secret_key):
    result = 0
    if len(files) > 0:
        if not os.path.isdir(fetch_cwd):
            os.makedirs(fetch_cwd)
        fetch_cmd = ["/usr/local/bin/s3funnel"]
        fetch_cmd.append(bucket_name)
        fetch_cmd.append("get")
        fetch_cmd.append("-a")
        fetch_cmd.append(aws_key)
        fetch_cmd.append("-s")
        fetch_cmd.append(aws_secret_key)
        fetch_cmd.append("-t")
        fetch_cmd.append("8")
        start = datetime.now()
        result = subprocess.call(fetch_cmd + files, cwd=fetch_cwd)
        duration_sec = timer.delta_sec(start)
        # TODO: verify MD5s
        downloaded_bytes = sum([ os.path.getsize(os.path.join(fetch_cwd, f)) for f in files ])
        downloaded_mb = downloaded_bytes / 1024.0 / 1024.0
        print "Downloaded %.2fMB in %.2fs (%.2fMB/s)" % (downloaded_mb, duration_sec, downloaded_mb / duration_sec)
    return result

def wait_for(processes, label):
    print "Waiting for", label, "..."
    for p in processes:
        p.join()
    print label, "Done."

class PipeStep(object):
    PAUSE_LENGTH = 5
    RETRIES = 3
    def __init__(self, num, name, q_in, q_out=None):
        self.num = num
        self.label = " ".join((name, str(num)))
        self.q_in = q_in
        self.q_out = q_out
        self.records_read = 0
        self.records_written = 0

        # Do stuff.
        self.setup()
        self.work()
        self.finish()
    def setup(self):
        pass
    def finish(self):
        print self.label, "All done, read", self.records_read, "records, wrote", self.records_written, "records"
        pass
    def handle(self, record):
        pass
    def work(self):
        print self.label, "Starting up"
        retries = PipeStep.RETRIES
        while True:
            try:
                raw = self.q_in.get(True, PipeStep.PAUSE_LENGTH)
                self.handle(raw)
                self.records_read += 1
            except Q.Empty:
                if retries > 0:
                    print self.label, "- Get timed out, trying again"
                    retries -= 1
                else:
                    break

class ReadRawStep(PipeStep):
    def __init__(self, num, name, raw_files, q_raw, schema):
        self.schema = schema
        PipeStep.__init__(self, num, name, raw_files, q_raw)

    def setup(self):
        self.expected_dim_count = len(self.schema._dimensions)

    def handle(self, raw_file):
        print self.label, "reading", raw_file
        try:
            fin = open(raw_file, "rb")
            bytes_read = 0
            record_count = 0
            start = datetime.now()
            while True:
                # Read two 4-byte values and one 8-byte value
                lengths = fin.read(16)
                if lengths == '':
                    break
                record_count += 1
                len_path, len_data, timestamp = struct.unpack("<IIQ", lengths)

                # Incoming timestamps are in milliseconds, so convert to POSIX first
                # (ie. seconds)
                submission_date = date.fromtimestamp(timestamp / 1000).strftime("%Y%m%d")
                path = unicode(fin.read(len_path), errors="replace")
                #print "Path for record", record_count, path, "length of data:", len_data

                # Detect and handle gzipped data.
                data = fin.read(len_data)
                if ord(data[0]) == 0x1f and ord(data[1]) == 0x8b:
                    # Data is gzipped, uncompress it:
                    try:
                        # Note: from brief testing, cStringIO doesn't appear to be any
                        #       faster. In fact, it seems slightly slower than StringIO.
                        data_reader = StringIO.StringIO(data)
                        uncompressor = gzip.GzipFile(fileobj=data_reader, mode="r")
                        data = unicode(uncompressor.read(), errors="replace")
                        uncompressor.close()
                        data_reader.close()
                    except Exception, e:
                        # Corrupted data, let's skip this record.
                        print self.label, "Warning: Found corrupted data for record", record_count, "in", raw_file, "path:", path
                        continue
                elif data[0] != "{":
                    # Data looks weird, should be JSON.
                    print self.label, "Warning: Found unexpected data for record", record_count, "in", raw_file, "path:", path, "data:"
                    print data

                bytes_read += 8 + len_path + len_data
                #print "Path for record", record_count, path, "length of data:", len_data, "data:", data[0:5] + "..."

                path_components = path.split("/")
                if len(path_components) != self.expected_dim_count:
                    # We're going to pop the ID off, but we'll also add the submission,
                    # so it evens out.
                    print self.label, "Found an invalid path in record", record_count, path
                    continue

                key = path_components.pop(0)
                info = {}
                info["reason"] = path_components.pop(0)
                info["appName"] = path_components.pop(0)
                info["appVersion"] = path_components.pop(0)
                info["appUpdateChannel"] = path_components.pop(0)
                info["appBuildID"] = path_components.pop(0)
                dimensions = self.schema.dimensions_from(info, submission_date)
                self.q_out.put((key, dimensions, data))
                self.records_written += 1
            duration = timer.delta_sec(start)
            mb_read = bytes_read / 1024.0 / 1024.0
            print self.label, "- Read %d records %.2fMB in %.2fs (%.2fMB/s)" % (record_count, mb_read, duration, mb_read / duration)
        except Exception, e:
            # Corrupted data, let's skip this record.
            print self.label, "- Error reading raw data from ", raw_file, e


class ConvertRawRecordsStep(PipeStep):
    def __init__(self, num, name, q_in, q_out, q_bad, converter):
        self.q_bad = q_bad
        self.bytes_read = 0
        self.bytes_written = 0
        self.bad_records = 0
        self.converter = converter
        self.start_time = datetime.now()
        self.end_time = datetime.now()
        PipeStep.__init__(self, num, name, q_in, q_out)

    def handle(self, record):
        self.end_time = datetime.now()
        key, dims, data = record
        #print self.label, "got", key
        self.bytes_read += len(data)
        try:
            parsed_data, parsed_dims = self.converter.convert_json(data, dims[-1])
            # TODO: take this out if it's too slow
            for i in range(len(dims)):
                if dims[i] != parsed_dims[i]:
                    print self.label, "Record", self.records_read, "mismatched dimension", i, dims[i], "!=", parsed_dims[i]
            serialized_data = self.converter.serialize(parsed_data)
            self.q_out.put((key, parsed_dims, serialized_data))
            self.bytes_written += len(serialized_data)
            self.records_written += 1
        except BadPayloadError, e:
            #self.q_bad.put((key, dims, data, e.msg))
            print self.label, "Bad payload:", e.msg
            self.bad_records += 1
        except Exception, e:
            #self.q_bad.put((key, dims, data, str(e)))
            msg = str(e)
            if msg != "Missing in payload: info.revision":
                print self.label, "ERROR:", e
            self.bad_records += 1

    def finish(self):
        duration = timer.delta_sec(self.start_time, self.end_time)
        read_rate = self.records_read / duration
        mb_read = self.bytes_read / 1024.0 / 1024.0
        mb_read_rate = mb_read / duration
        write_rate = self.records_written / duration
        mb_written = self.bytes_written / 1024.0 / 1024.0
        mb_write_rate = mb_written / duration
        print "%s All done, read %d records or %.2fMB (%.2fr/s, %.2fMB/s), wrote %d or %.2f MB (%.2fr/s, %.2fMB/s). Found %d bad records" % (self.label, self.records_read, mb_read, read_rate, mb_read_rate, self.records_written, mb_written, write_rate, mb_write_rate, self.bad_records)


class WriteConvertedStep(PipeStep):
    def __init__(self, num, name, q_in, q_out, storage):
        self.storage = storage
        self.bytes_written = 0
        self.start_time = datetime.now()
        self.end_time = datetime.now()
        PipeStep.__init__(self, num, name, q_in, q_out)

    def handle(self, record):
        key, dims, data = record
        n = self.storage.write(key, data, dims)
        # TODO: write out completed files as we see them
        #if n.endswith(StorageLayout.PENDING_COMPRESSION_SUFFIX):
        #    q_out.put(n)
        self.records_written += 1
        self.bytes_written += len(data)


class ExportCompletedStep(PipeStep):
    def handle(self, record):
        print self.label, "got a record"



def main():
    parser = argparse.ArgumentParser(description='Process incoming Telemetry data', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("incoming_bucket", help="The S3 bucket containing incoming files")
    parser.add_argument("publish_bucket", help="The S3 bucket to save processed files")
    parser.add_argument("-k", "--aws-key", help="AWS Key", required=True)
    parser.add_argument("-s", "--aws-secret-key", help="AWS Secret Key", required=True)
    parser.add_argument("-w", "--work-dir", help="Location to cache downloaded files", required=True)
    parser.add_argument("-o", "--output-dir", help="Base dir to store processed data", required=True)
    parser.add_argument("-i", "--input-files", help="File containing a list of keys to process", type=file)
    parser.add_argument("-c", "--histogram-cache-path", help="Path to store a local cache of histograms", default="./histogram_cache")
    parser.add_argument("-t", "--telemetry-schema", help="Location of the desired telemetry schema", required=True)
    parser.add_argument("-m", "--max-output-size", metavar="N", help="Rotate output files after N bytes", type=int, default=500000000)
    args = parser.parse_args()

    schema_data = open(args.telemetry_schema)
    schema = TelemetrySchema(json.load(schema_data))
    schema_data.close()

    cache = RevisionCache(args.histogram_cache_path, "hg.mozilla.org")
    converter = Converter(cache, schema)

    storage = StorageLayout(schema, args.output_dir, args.max_output_size)

    #num_cpus = multiprocessing.cpu_count()
    num_cpus = 2

    start = datetime.now()
    conn = S3Connection(args.aws_key, args.aws_secret_key)
    incoming_bucket = conn.get_bucket(args.incoming_bucket)
    incoming_filenames = []
    if args.input_files:
        print "Fetching file list from file", args.input_files
        incoming_filenames = [ l.strip() for l in args.input_files.readlines() ]
    else:
        print "Fetching file list from S3..."
        for f in incoming_bucket.list():
            incoming_filenames.append(f.name)
    print "Done"

    for f in incoming_filenames:
        print "  ", f
    

    result = 0
    print "Downloading", len(incoming_filenames), "files..."
    result = 0#fetch_s3_files(incoming_filenames, args.work_dir, args.incoming_bucket, args.aws_key, args.aws_secret_key)
    if result != 0:
        print "Error downloading files. Return code of s3funnel was", result
        return result
    print "Done"

    local_filenames = [os.path.join(args.work_dir, f) for f in incoming_filenames]

    # TODO: try a SimpleQueue
    raw_files = Queue(1000)
    for l in local_filenames:
        raw_files.put(l)

    raw_records = Queue(10000)
    converted_records = Queue(20000)
    bad_records = Queue()
    completed_files = Queue()
    compressed_files = Queue()

    # Begin reading raw input
    raw_readers = []
    for i in range(num_cpus):
        rr = Process(
                target=ReadRawStep,
                args=(i, "Reader", raw_files, raw_records, schema))
        raw_readers.append(rr)
        rr.start()
        print "Reader", i, "pid:", rr.pid
    print "Readers all started"

    # Convert raw input as it becomes available
    converters = []
    for i in range(num_cpus):
        cr = Process(
                target=ConvertRawRecordsStep,
                args=(i, "Converter", raw_records, converted_records, bad_records, converter))
        converters.append(cr)
        cr.start()
        print "Converter", i, "pid:", cr.pid
    print "Converters all started"

    # Writer converted data as it becomes available
    writers = []
    for i in range(num_cpus):
        w = Process(
                target=WriteConvertedStep,
                args=(i, "Writer", converted_records, completed_files, storage))
        writers.append(w)
        w.start()
        print "Writer", i, "pid:", w.pid
    print "Writers all started"

    # Compress and export completed files.
    exporters = []
    for i in range(num_cpus):
        e = Process(
                target=ExportCompletedStep,
                args=(i, "Exporter", completed_files))
        exporters.append(e)
        e.start()
        print "Exporter", i, "pid:", e.pid
    print "Exporters all started"

    # Wait for raw input to complete.
    wait_for(raw_readers, "Raw Readers")

    # Wait for conversion to complete.
    wait_for(converters, "Converters")

    wait_for(writers, "Converted Writers")

    # TODO: find <out_dir> -type f -not -name ".compressme"
    # Add them to completed_files

    wait_for(exporters, "Exporters to S3")

    print "Removing processed logs from S3..."
    for f in incoming_filenames:
        print "  Deleting", f
        #incoming_bucket.delete_key(f)
    print "Done"

    duration = timer.delta_sec(start)
    print "All done in %.2fs" % (duration)
    return 0

if __name__ == "__main__":
    sys.exit(main())
