from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple, Type, Union


class InvalidConfigKey(KeyError):
    """Raised when a config key is not (or no longer) part of the schema."""


class InvalidConfigValue(ValueError):
    """Raised when a value does not match the type(s) declared in the schema."""


@dataclass(frozen=True)
class ConfigField:
    """
    Schema entry for a single config key.

    `type` may be a single type or a tuple of types; if omitted it's inferred
    from `default`'s type, which covers the common case. Pass it explicitly
    for keys where the default doesn't tell the whole story, e.g.
    ``ConfigField(default=None, type=(int, type(None)))``.
    """

    default: Any
    type: Union[Type, Tuple[Type, ...]] = field(default=None)
    description: str = ""

    def __post_init__(self):
        types = self.type if self.type is not None else type(self.default)
        if not isinstance(types, tuple):
            types = (types,)
        object.__setattr__(self, "type", types)


class Config:
    """
    Dict-like config manager backed by the plugin's database partition.

    Keys are declared in :attr:`defaults` as::

        {"key_name": ConfigField(default=True, description="...")}

    ``defaults`` is meant to change across plugin versions (new options get
    added, old ones get removed). :meth:`fetch` reconciles whatever is
    currently stored in the database against the schema every time it runs:
    keys that are missing get created with their default value, keys that
    are no longer declared get dropped. Existing installations therefore
    migrate automatically the next time the plugin loads, without manual
    intervention.
    """

    defaults: Dict[str, ConfigField] = {
        "require_claim": ConfigField(
            default=True,
            description="Whether a thread must be claimed before someone can reply to it.",
        ),
    }

    def __init__(self, db, bot):
        self.db = db
        self.bot = bot

        self._cache: Dict[str, Any] = {}
        self._loaded = False

    def __repr__(self):
        return f"<Config {self._cache!r}>"

    async def fetch(self) -> Dict[str, Any]:
        """
        Loads the config document from the database and reconciles it with
        :attr:`defaults`, adding missing keys and dropping ones that are no
        longer part of the schema. Persists the reconciled document if
        anything changed. Populates and returns the cache.
        """
        document = await self.db.find_one({"_id": "config"})
        stored = {k: v for k, v in (document or {}).items() if k != "_id"}

        changed = document is None

        for key, schema in self.defaults.items():
            if key not in stored:
                stored[key] = deepcopy(schema.default)
                changed = True

        for key in list(stored.keys()):
            if key not in self.defaults:
                del stored[key]
                changed = True

        self._cache = stored
        self._loaded = True

        if changed:
            await self.save()

        return self._cache

    async def save(self) -> None:
        """
        Persists the current cache to the database, replacing the stored
        document wholesale. A full replace (rather than `$set`) is required
        so that keys dropped from `defaults` actually disappear from the
        database instead of lingering there forever, since `$set` never
        removes fields.
        """
        await self.db.replace_one(
            {"_id": "config"},
            {"_id": "config", **self._cache},
            upsert=True,
        )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("Config has not been loaded yet, call `await config.fetch()` first.")

    def _ensure_valid_key(self, key: str) -> None:
        if key not in self.defaults:
            raise InvalidConfigKey(f"Unknown config key: {key!r}")

    def validate(self, key: str, value: Any) -> None:
        """Raises :class:`InvalidConfigKey`/:class:`InvalidConfigValue` if `key`/`value` don't match the schema."""
        self._ensure_valid_key(key)
        expected_types = self.defaults[key].type
        if not isinstance(value, expected_types):
            expected = ", ".join(t.__name__ for t in expected_types)
            raise InvalidConfigValue(
                f"Config key {key!r} expects type(s) {expected}, got {type(value).__name__}."
            )

    def describe(self, key: str) -> str:
        """Returns the human-readable description declared for `key`."""
        self._ensure_valid_key(key)
        return self.defaults[key].description

    def get(self, key: str, default: Any = None) -> Any:
        """Returns the cached value for `key`, or `default` if the key isn't set yet."""
        self._ensure_loaded()
        self._ensure_valid_key(key)
        if key not in self._cache:
            return default
        return self._cache[key]

    async def set(self, key: str, value: Any, *, save: bool = True) -> Any:
        """Validates and stores `value` under `key`, persisting it unless `save=False`."""
        self._ensure_loaded()
        self.validate(key, value)
        self._cache[key] = value
        if save:
            await self.save()
        return value

    async def reset(self, key: str, *, save: bool = True) -> Any:
        """Resets `key` back to its schema default."""
        self._ensure_loaded()
        self._ensure_valid_key(key)
        value = deepcopy(self.defaults[key].default)
        self._cache[key] = value
        if save:
            await self.save()
        return value

    def all(self) -> Dict[str, Any]:
        """Returns a shallow copy of the full cached config."""
        self._ensure_loaded()
        return dict(self._cache)

    def keys(self):
        self._ensure_loaded()
        return self._cache.keys()

    def values(self):
        self._ensure_loaded()
        return self._cache.values()

    def items(self):
        self._ensure_loaded()
        return self._cache.items()

    def __getitem__(self, key: str) -> Any:
        self._ensure_loaded()
        self._ensure_valid_key(key)
        return self._cache[key]

    def __setitem__(self, key: str, value: Any) -> None:
        # Sync convenience setter: validates and updates the cache only.
        # Use `await config.set(key, value)` when the change must be persisted.
        self._ensure_loaded()
        self.validate(key, value)
        self._cache[key] = value

    def __delitem__(self, key: str) -> None:
        # Keys are schema-defined, so "deleting" resets to the schema default
        # instead of actually removing the entry (in memory only, use
        # `await config.reset(key)` to persist).
        self._ensure_loaded()
        self._ensure_valid_key(key)
        self._cache[key] = deepcopy(self.defaults[key].default)

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    def __iter__(self):
        self._ensure_loaded()
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)
