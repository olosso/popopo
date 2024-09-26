#!/home/olli/mambaforge/envs/async/bin/python3
import os
import asyncio
import logging
from datetime import datetime, timedelta
import functools
from enum import Flag, auto
import argparse
import re
import csv
import pathlib

logging.basicConfig(
    filename="server.log",
    format="%(asctime)s - %(levelname)s: %(message)s",
    level=logging.DEBUG,
)


class Interval(Flag):
    WORK = auto()
    SHORT_BREAK = auto()
    LONG_BREAK = auto()

    def duration(self):
        dur = 0
        match self:
            case Interval.WORK:
                dur = 45 * 60
            case Interval.SHORT_BREAK:
                dur = 10 * 60
            case Interval.LONG_BREAK:
                dur = 20 * 60

        return dur


def empty_or_none(s):
    return s == None or s == ""


class State:
    def __init__(self, description):

        self.prev = datetime.now()
        self.elapsed = timedelta(0)

        self.start_time = datetime.now()
        self.end_time = datetime.now()

        self.interval = Interval.WORK
        self.duration = self.interval.duration()
        self.description = description
        self.work_intervals = 0

        self.is_paused = False
        self.shutdown = False

    def minutes(self):
        return int(self.elapsed.seconds / 60)

    def zero(self):
        self.prev = datetime.now()
        self.elapsed = timedelta(0)

        self.start_time = datetime.now()
        self.end_time = datetime.now()

    def cycle(self):
        log_msg = f"Ending interval {str(self.interval)}"
        logging.info(log_msg)
        match self.interval:
            case Interval.WORK:
                self.record()
                log_msg = f"Taking a break from {self.description}"
                logging.info(log_msg)
                self.work_intervals += 1
                if self.work_intervals == 2:
                    self.interval = Interval.LONG_BREAK
                    self.work_intervals = 0
                else:
                    self.interval = Interval.SHORT_BREAK
            case Interval.LONG_BREAK | Interval.SHORT_BREAK:
                log_msg = f"Starting work on {self.description}"
                logging.info(log_msg)
                self.interval = Interval.WORK
                self.start_time = datetime.now()

        self.zero()
        self.duration = self.interval.duration()

    def record(self):
        self.end_time = datetime.now()
        assert (self.end_time - self.start_time).total_seconds() >= 0
        with open("popopo.csv", "a", newline="") as f:
            writer = csv.writer(
                f, delimiter=";", quotechar="|", quoting=csv.QUOTE_MINIMAL
            )
            description = (
                self.description if not empty_or_none(self.description) else "undefined"
            )
            pause_or_work = "pause" if self.is_paused else "work"
            writer.writerow(
                [
                    self.start_time,
                    self.end_time,
                    description,
                    pause_or_work,
                ]
            )

    def pause(self):
        if self.is_paused:
            return
        if self.interval == Interval.WORK:
            self.record()
            self.start_time = datetime.now()
        self.is_paused = True

    def unpause(self):
        if not self.is_paused:
            return
        if self.interval == Interval.WORK:
            self.record()
            self.start_time = datetime.now()
        self.is_paused = False

    def update(self, delta_time):
        if self.is_paused:
            return
        self.elapsed += delta_time

        if self.elapsed.seconds >= self.duration:
            self.cycle()

    def __str__(self):

        max_mins = int(self.duration / 60.0)
        msg = f"{self.minutes()}m / {max_mins}m"
        icon = ""

        if self.is_paused:
            icon = "⏸"
            return f"'{icon} {msg}'"

        match self.interval:
            case Interval.WORK:
                icon = "🍅"
            case Interval.SHORT_BREAK:
                icon = "🧍"
            case Interval.LONG_BREAK:
                icon = "💤"
        return f"'{icon} {msg}'"


async def _listen(reader, writer, /, state):

    data = await reader.read(1024)
    decoded = data.decode().strip()
    logging.info(f"Received: {decoded}")
    response = ""
    match decoded:
        case "continue":
            state.unpause()
            response = f"🏃 Time remaining: {int((state.duration - state.elapsed.seconds)/60)} minutes"
        case "kill":
            state.shutdown = True
            response = "💤 Shutting server down."
        case "pause":
            state.pause()
            response = f"⏱ Paused at {state.elapsed}"
        case "reset":
            state.zero()
            response = "🔄 Resetting timer."
        case "time":
            response = f"⏲ Current interval has run for {state.minutes()} minutes"
        case "skip":
            response = f"⏯ Skipping {state.interval}."
            state.cycle()
        case str(x) if x.startswith("set"):
            logging.info(f"Got to set.")
            description = re.search("--description '(.*)'", decoded)
            if description:
                state.description = description.group(1)
                response = f"📑 Set description to '{state.description}'."
            else:
                if state.description is None or state.description == "":
                    response = f"🙉 Current description is empty."
                else:
                    response = f"📖 Current description is '{state.description}'."
        case _:
            response = decoded
    if response != "":
        try:
            writer.write(response.encode("utf-8"))
            await writer.drain()
            logging.info(f"Responded: {response}")
        except ConnectionResetError:
            logging.warning("Client broke connection.")

    logging.info(f"Request handled.")


def qtile(text):
    return " ".join(
        [
            "qtile",
            "cmd-obj",
            "-o",
            "widget",
            "popopo",
            "-f",
            "update",
            "-a",
            text,
        ]
    )


async def serve(socket_path, description):
    state = State(description)

    try:

        listen = functools.partial(_listen, state=state)
        task = asyncio.create_task(asyncio.start_unix_server(listen, path=socket_path))

        while not state.shutdown:
            await asyncio.sleep(1)

            now = datetime.now()
            delta = now - state.prev
            state.prev = now

            state.update(delta)

            command = qtile(str(state))
            await asyncio.create_subprocess_shell(command)

        task.cancel()

    except Exception as err:
        print(f"Unexpected {err=}, {type(err)=}")

    finally:
        if os.path.exists(socket_path):
            os.remove(socket_path)
        command = qtile('"🍅 -- / --"')
        await asyncio.create_subprocess_shell(command)

    logging.info("Terminating popopo server")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--description", help="task description")
    args = parser.parse_args()
    description = args.description or ""

    dir = str(pathlib.Path(__file__).parent.resolve()) + "/"
    socket_path = dir + "popopo_socket"
    asyncio.run(serve(socket_path, description))

    if os.path.exists(socket_path):
        os.remove(socket_path)
