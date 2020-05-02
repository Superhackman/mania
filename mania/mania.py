import sys
import os
import argparse
import requests
from typing import List, Optional, Type, Union

import questionary
import toml
from tqdm import tqdm

from . import constants
from .models import Client, Track, Album, Artist
from . import metadata
from .tidal import TidalClient


class ManiaException(Exception):
    exit_code = 0


class ManiaSeriousException(ManiaException):
    exit_code = 1


def log(config: dict, message: str = "", indent: int = 0) -> None:
    if message is not None and not config["quiet"]:
        print(constants.INDENT * indent + str(message))


def sanitize(config: dict, string: str) -> str:
    if not config["nice-format"]:
        illegal_characters = frozenset("/")
        return "".join(c for c in string if c not in illegal_characters)
    alphanumeric = "".join(c for c in string if c.isalnum() or c in (" ", "-"))
    hyphenated = alphanumeric.replace(" ", "-")
    return "-".join(word for word in hyphenated.split("-") if word).lower()


def search(
    client: Client,
    config: dict,
    media_type: Type[Union[Track, Album, Artist]],
    query: str,
) -> Union[Track, Album, Artist]:
    log(config, "Searching...")
    string = " ".join(query)
    results = client.search(string, media_type, config["search-count"])
    if not results:
        raise ManiaSeriousException("No results found.")
    if config["lucky"]:
        return results[0]

    def label_track(track: Track) -> str:
        name = track.name
        artists = ", ".join([artist.name for artist in track.artists])
        album = track.album.name
        indent = constants.INDENT + " " * 3
        year = track.album.year
        if year:
            return f"{name}\n{indent}{artists}\n{indent}{album} ({year})"
        else:
            return f"{name}\n{indent}{artists}\n{indent}{album}"

    def label_album(album: Album) -> str:
        name = album.name
        artists = ", ".join([artist.name for artist in album.artists])
        indent = constants.INDENT + " " * 3
        year = album.year
        if year:
            return f"{name} ({year})\n{indent}{artists}"
        else:
            return f"{name}\n{indent}{artists}\n"

    def label_artist(artist: Artist) -> str:
        return artist.name

    labeler = {Track: label_track, Album: label_album, Artist: label_artist,}[
        media_type
    ]

    choices = [questionary.Choice(labeler(result), value=result) for result in results]
    answer = questionary.select("Select one:", choices=choices).ask()
    if not answer:
        raise ManiaException("")
    return answer


def resolve_metadata(config: dict, track: Track, path: str, indent: int) -> None:
    log(config, "Resolving metadata...", indent=indent)

    cover: Optional[metadata.Cover]
    if track.album.cover_url:
        request = requests.get(track.album.cover_url)
        request.raise_for_status()
        data = request.content
        mime = request.headers.get("Content-Type", "")
        cover = metadata.Cover(data, mime)
    else:
        cover = None

    {"mp4": metadata.resolve_mp4_metadata, "flac": metadata.resolve_flac_metadata}[
        track.extension
    ](config, track, path, cover)


def get_track_path(
    client: Client,
    config: dict,
    track: Track,
    siblings: List[Track] = None,
    include_artist: bool = False,
    include_album: bool = False,
) -> str:
    artist_path = ""
    album_path = ""
    disc_path = ""
    file_path = ""
    if include_artist or config["full-structure"]:
        artist_path = sanitize(config, track.album.artists[0].name)

    if include_album or config["full-structure"]:
        siblings = siblings or client.get_album_tracks(track.album)
        maximum_disc_number = max([sibling.disc_number for sibling in siblings])
        maximum_track_number = max([sibling.track_number for sibling in siblings])
        album_path = sanitize(config, track.album.name)
        if maximum_disc_number > 1:
            disc_number = str(track.disc_number).zfill(len(str(maximum_disc_number)))
            disc_path = sanitize(config, f"Disc {disc_number}")
        track_number = str(track.track_number).zfill(len(str(maximum_track_number)))
        file_path = sanitize(config, f"{track_number} {track.name}")
    else:
        file_path = sanitize(config, track.name)

    return os.path.join(
        config["output-directory"], artist_path, album_path, disc_path, file_path
    )


def download_track(
    client: Client,
    config: dict,
    track: Track,
    siblings: List[Track] = None,
    include_artist: bool = False,
    include_album: bool = False,
    indent: int = 0,
) -> None:
    track_path = get_track_path(
        client,
        config,
        track,
        siblings=siblings,
        include_artist=include_artist,
        include_album=include_album,
    )
    temporary_path = f"{track_path}.{constants.TEMPORARY_EXTENSION}.{track.extension}"
    final_path = f"{track_path}.{track.extension}"
    if os.path.isfile(final_path):
        log(
            config,
            f"Skipping download of {os.path.basename(final_path)}; it already exists.",
            indent=indent,
        )
        return
    try:
        try:
            media_url, decryptor = client.get_media(track)
        except requests.exceptions.HTTPError as error:
            if error.response.status_code == 429:
                # re-authenticate and retry if we receive a 429 Client Error: Too Many Requests for url
                log(
                    config, f"Too many requests, logging out and back in...", indent=indent,
                )
                client.authenticate()
                media_url, decryptor = client.get_media(track)
            else:
                raise error

    except requests.exceptions.HTTPError as error:
        status_code = error.response.status_code
        sub_status = error.response.json().get("subStatus")
        if (status_code, sub_status) == (401, 4005):
            log(
                config,
                f"Skipping download of {os.path.basename(final_path)}; track is not available.",
                indent=indent,
            )
            return
        raise error
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    request = requests.get(media_url, stream=True)
    request.raise_for_status()
    with open(temporary_path, mode="wb") as temp_file:
        chunk_size = constants.DOWNLOAD_CHUNK_SIZE
        iterator = request.iter_content(chunk_size=chunk_size)
        if config["quiet"]:
            for chunk in iterator:
                temp_file.write(chunk)
        else:
            total = int(request.headers["Content-Length"])
            with tqdm(
                total=total,
                miniters=1,
                unit="B",
                unit_divisor=1024,
                unit_scale=True,
                dynamic_ncols=True,
            ) as progress_bar:
                for chunk in iterator:
                    temp_file.write(chunk)
                    progress_bar.update(chunk_size)

    if decryptor:
        log(config, "Decrypting...", indent=indent)
        decryptor(temporary_path)

    if not config["skip-metadata"]:
        try:
            resolve_metadata(config, track, temporary_path, indent)
        except metadata.InvalidFileError:
            log(
                config,
                f"Skipping {os.path.basename(final_path)}; received invalid file",
                indent=indent,
            )
            os.remove(temporary_path)
            return
    os.rename(temporary_path, final_path)


def handle_track(client: Client, config: dict, query: str) -> None:
    track = search(client, config, Track, query)
    log(config, f'Downloading "{track.name}"...')
    download_track(client, config, track)


def handle_album(client: Client, config: dict, query: str) -> None:
    album = search(client, config, Album, query)
    log(config, f'Downloading "{album.name}"...')
    download_album(client, config, album)


def download_album(
    client: Client,
    config: dict,
    album: Album,
    include_artist: bool = False,
    indent: int = 0,
) -> None:
    tracks = client.get_album_tracks(album)
    for index, track in enumerate(tracks, 1):
        log(
            config,
            f'Downloading "{track.name}" ({index} of {len(tracks)} track(s))...',
            indent=indent,
        )
        download_track(
            client,
            config,
            track,
            siblings=tracks,
            include_artist=include_artist,
            include_album=True,
            indent=indent + 1,
        )


def handle_discography(client: Client, config: dict, query: str) -> None:
    artist = search(client, config, Artist, query)
    log(config, f'Downloading "{artist.name}"...')
    download_discography(client, config, artist)


def download_discography(
    client: Client, config: dict, artist: Artist, indent: int = 0
) -> None:
    albums = client.get_artist_albums(artist)
    for index, album in enumerate(albums, 1):
        log(
            config,
            f'Downloading "{album.name}" ({index} of {len(albums)} album(s))...',
            indent=indent,
        )
        download_album(client, config, album, include_artist=True, indent=indent + 1)


def load_config(args: dict) -> dict:
    if args["config-path"]:
        config_path = args["config-path"]
    else:
        config_path = constants.CONFIG_PATH
        if not os.path.isfile(config_path):
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w") as config_file:
                config_file.write(constants.DEFAULT_CONFIG)

    config_toml = toml.load(config_path)

    def resolve(from_args, from_file, default):
        if from_args is not None:
            return from_args
        if from_file is not None:
            return from_file
        return default

    config = {
        key: resolve(args.get(key), config_toml.get(key), default)
        for key, default in constants.DEFAULT_CONFIG_TOML.items()
    }
    config["output-directory"] = os.path.expanduser(config["output-directory"])
    config["config-path"] = config_path
    return config


def run() -> None:
    parser = argparse.ArgumentParser()
    handlers = {
        "track": handle_track,
        "track": handle_track,
        "album": handle_album,
        "artist": handle_discography,
        "discography": handle_discography,
    }
    subparsers = parser.add_subparsers(dest="query")
    subparsers.required = True
    for name, handler in handlers.items():
        subparser = subparsers.add_parser(name)
        subparser.add_argument("query", nargs="+")
        for key, value in constants.DEFAULT_CONFIG_TOML.items():
            if isinstance(value, bool):
                boolean = subparser.add_mutually_exclusive_group()

                # we don't use store_true/store_false here because we need the
                # default value to be None, not False/True.
                boolean.add_argument(
                    f"--{key}", action="store_const", const=True, dest=key
                )
                boolean.add_argument(
                    f"--no-{key}", action="store_const", const=False, dest=key
                )
            else:
                subparser.add_argument(f"--{key}", nargs="?", dest=key)
        subparser.add_argument("--config-path", dest="config-path")
        subparser.set_defaults(func=handler)

    parsed_args = parser.parse_args()
    args = vars(parsed_args)
    config = load_config(args)

    client = TidalClient(config)

    log(config, "Authenticating...")
    try:
        client.authenticate()
    except requests.exceptions.HTTPError as error:
        if error.response.status_code in (400, 401):
            data = error.response.json()
            message = data["userMessage"]
            raise ManiaSeriousException(f"Authentication failed: {message}")
        raise error

    parsed_args.func(client, config, args["query"])
    log(config, "Done!")


def main() -> None:
    try:
        run()
    except ManiaException as exception:
        if str(exception):
            print(exception, file=sys.stderr)
        sys.exit(exception.exit_code)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()