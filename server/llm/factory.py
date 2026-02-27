from .provider import LLMProvider, LLMProviderError

# Registry of provider name -> callable that returns an LLMProvider instance.
# Each entry is a zero-arg factory so that imports and API-key validation are
# deferred until the provider is actually requested.
_PROVIDERS = {
    "gemini": lambda: _make_gemini(),
}


def _make_gemini() -> LLMProvider:
    from .gemini_provider import GeminiProvider
    return GeminiProvider()


class LLMFactory:
    """Instantiate an :class:`LLMProvider` by name.

    Supported names (case-insensitive):
        * ``"gemini"`` — Google Gemini (requires ``GEMINI_API_KEY``)

    Future providers (``"openai"``, ``"claude"``, ...) can be added by
    registering an entry in ``_PROVIDERS``.
    """

    @staticmethod
    def get_provider(name: str) -> LLMProvider:
        """Return a ready-to-use provider instance.

        Raises:
            LLMProviderError: if *name* is unknown or construction fails.
        """
        key = name.strip().lower()
        builder = _PROVIDERS.get(key)
        if builder is None:
            supported = ", ".join(sorted(_PROVIDERS))
            raise LLMProviderError(
                f"Unknown LLM provider '{name}'. Supported: {supported}"
            )
        return builder()
