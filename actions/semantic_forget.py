"""Local semantic deletion helper for JARVIS memory."""


def install(memory, original_forget):
    """Wrap memory.forget with a local semantic fallback."""
    def forget(text):
        removed = original_forget(text)
        if removed:
            return removed

        cleaned = memory.safety.clean(text, 200)
        if not cleaned:
            return 0

        candidates = [cleaned]
        if cleaned.casefold().startswith(("on ", "about ")):
            candidates.append(cleaned.split(" ", 1)[1].strip())

        try:
            from actions import semantic_memory

            documents = semantic_memory._documents()

            for candidate in candidates:
                matches = semantic_memory._semantic_rank(candidate, documents)

                if not matches:
                    continue

                index, score = matches[0]

                if score < 0.62:
                    continue

                _key, value, display = documents[index]
                target = display if display else value
                removed = original_forget(target)

                if removed:
                    return removed

        except Exception as error:
            print(f"[JARVIS] semantic memory forget failed: {error}")

        return 0

    memory.forget = forget
