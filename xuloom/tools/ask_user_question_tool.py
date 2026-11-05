"""
AskUserQuestionTool — structured user interaction via multiple-choice questions.

Presents structured questions with options and returns the user's actual answers.
"""

import copy
import json
from typing import List
from pydantic import BaseModel, ConfigDict, Field, field_validator
from xuloom.tools.base import BaseAgenticTool


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(..., min_length=1, description="Short user-facing answer label. This belongs inside a question's options array.")
    description: str = Field("", description="Optional longer description.")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(..., min_length=1, description="Required: the full question to present. This object represents ONE question, not one answer option.")
    options: List[QuestionOption] = Field(
        ...,
        min_length=2,
        max_length=4,
        description="Required: 2–4 answer objects, each with label and optional description. Put ALL answers for this question here, never directly in questions. The UI provides custom text input automatically; do not add an Other option.",
    )
    multiSelect: bool = Field(
        False,
        description="If true, allow the user to select multiple options.",
    )

    @field_validator("options")
    @classmethod
    def unique_labels(cls, options):
        if len({option.label for option in options}) != len(options):
            raise ValueError("Option labels must be unique within a question.")
        return options


class AskUserQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: List[Question] = Field(
        ...,
        min_length=1,
        max_length=4,
        description="1–4 QUESTION objects. Each must contain question (text) and options (2–4 answers). One question with four choices is ONE item here, with FOUR items inside its options.",
    )

    @field_validator("questions")
    @classmethod
    def unique_questions(cls, questions):
        if len({item.question for item in questions}) != len(questions):
            raise ValueError("Question texts must be unique within a request.")
        return questions


QUESTION_EXAMPLE = {"questions": [{"question": "Which approach do you prefer?", "options": [
    {"label": "Minimal change (Recommended)", "description": "Fix the immediate issue."},
    {"label": "Broader change", "description": "Also revise related behavior."}], "multiSelect": False}]}
QUESTION_SHAPE = (
    'Each questions item must be {"question":"...","options":[{"label":"...","description":"..."},'
    '{"label":"...","description":"..."}],"multiSelect":false}. '
    'Do not place label/description directly in questions. '
)


class AskUserQuestionTool(BaseAgenticTool):
    name = "ask_user_question_tool"
    description = (
        "Asks the user multiple choice questions to gather information, "
        "clarify ambiguity, understand preferences, make decisions or offer them choices. "
        "Call this tool to display clickable choices; printing a list in chat does not display the picker. "
        + QUESTION_SHAPE
    )
    input_schema = AskUserQuestionInput
    risk_level = "low"
    is_read_only = True

    @classmethod
    def get_tool_definition(cls):
        definition = super().get_tool_definition()
        schema = definition["input_schema"]
        definitions = schema.pop("$defs", {})

        def inline(value):
            if isinstance(value, list):
                return [inline(item) for item in value]
            if not isinstance(value, dict):
                return value
            value = copy.deepcopy(value)
            reference = value.pop("$ref", None)
            if reference:
                value = {**copy.deepcopy(definitions[reference.rsplit("/", 1)[-1]]), **value}
            return {key: inline(item) for key, item in value.items()}

        # These models are non-recursive. Keep their nested fields visible even
        # to providers that handle local JSON Schema references poorly.
        definition["input_schema"] = inline(schema)
        definition["input_schema"]["examples"] = [copy.deepcopy(QUESTION_EXAMPLE)]
        return definition

    def is_concurrency_safe_call(self, **kwargs) -> bool:
        # One picker at a time; read-only does not imply interactive concurrency.
        return False

    def input_error_hint(self) -> str:
        return QUESTION_SHAPE + "\nValid example (replace with the user's actual question and choices):\n" + json.dumps(QUESTION_EXAMPLE)

    def prompt(self) -> str:
        return """Use ask_user_question_tool when missing information or a user decision is needed. Ask only when the answer matters; do not turn routine tasks into questionnaires. If the user requests clickable choices, actually call this tool rather than printing choices or claiming a call occurred. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take

Usage notes:
- Users can always provide a custom text answer
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label
- Ask one concise question when possible. Keep labels short and put explanations in description.
- The UI provides custom text input; do not add a separate Other option.
- Wait for the actual tool result. Never invent the user's answer.

""" + self.input_error_hint() + """

Plan mode note: In plan mode, use this tool to clarify requirements or choose between approaches BEFORE finalizing your plan. Do NOT use this tool to ask "Is my plan ready?" or "Should I proceed?" — use exit_plan_mode for plan approval. IMPORTANT: Do not reference "the plan" in your questions (e.g., "Do you have feedback about the plan?", "Does the plan look good?") because the user cannot see the plan in the UI until you call exit_plan_mode. If you need plan approval, use exit_plan_mode instead."""

    def build_permission_summary(self, questions: List[dict] = None) -> str:
        questions = questions or []
        lines = [f"Asking {len(questions)} question(s):"]
        for i, q in enumerate(questions[:4]):
            text = q.get("question", "")[:100]
            opt_count = len(q.get("options", []))
            lines.append(f"  Q{i+1}: {text} ({opt_count} options)")
        return "\n".join(lines)

    async def execute(self, questions: List[dict] = None) -> str:
        """
        Present questions to the user and collect answers.

        In CLI mode this uses rich prompts; in SDK mode it returns the
        question payload for the host to render.
        """
        questions = questions or []
        ui_callback = self.context.get("ask_user_callback")

        if ui_callback is None:
            return self._fallback_text_prompt(questions)

        import inspect
        result = ui_callback(questions)
        if inspect.isawaitable(result):
            result = await result

        if isinstance(result, dict):
            return self._format_answers(questions, result)
        return str(result)

    def _fallback_text_prompt(self, questions: list) -> str:
        """Synchronous fallback when no UI callback is registered."""
        answers = {}
        for i, q in enumerate(questions):
            q_text = q.get("question", f"Question {i+1}")
            options = q.get("options", [])
            multi = q.get("multiSelect", False)

            prompt_text = f"\n{'─'*60}\n  {q_text}\n"
            for j, opt in enumerate(options):
                label = opt.get("label", f"Option {j+1}")
                desc = opt.get("description", "")
                desc_str = f" — {desc}" if desc else ""
                prompt_text += f"    [{j+1}] {label}{desc_str}\n"
            prompt_text += "    [0] Other (type custom answer)\n"

            if multi:
                raw = input(prompt_text + "  Select one or more (comma-separated numbers): ").strip()
            else:
                raw = input(prompt_text + "  Select: ").strip()

            if not raw or raw == "0":
                custom = input("  Your answer: ").strip()
                answers[q_text] = custom
            else:
                try:
                    indices = [int(x.strip()) - 1 for x in raw.split(",")]
                    selected = []
                    for idx in indices:
                        if 0 <= idx < len(options):
                            selected.append(options[idx].get("label", ""))
                    answers[q_text] = selected if multi else (selected[0] if selected else raw)
                except (ValueError, IndexError):
                    answers[q_text] = raw

        return self._format_answers(questions, answers)

    def _format_answers(self, questions: list, answers: dict) -> str:
        lines = []
        for q in questions:
            q_text = q.get("question", "")
            answer = answers.get(q_text, "(no answer)")
            if isinstance(answer, list):
                answer_str = ", ".join(str(a) for a in answer)
            else:
                answer_str = str(answer)
            lines.append(f"Q: {q_text}\nA: {answer_str}")
        return "\n\n".join(lines)
