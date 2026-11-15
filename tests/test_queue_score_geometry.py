"""
Tests — géométrie du score de file (constat P2-16 de l'audit).

Le score Redis d'un ticket vaut `(10 - priorité) × MARGE + created_ms`.
La MARGE vaut 10⁹ ms, soit 11,57 jours : une valeur qui « ressemble » à des
millisecondes alors qu'elle représente plus de onze jours d'ancienneté. Un
futur refactor qui la remplacerait par `1000` (une seconde, l'erreur naturelle)
casserait la priorité sans qu'aucun test métier ne s'en aperçoive — d'où ce
fichier, qui verrouille l'invariant plutôt que la constante.

Invariant : un ticket de priorité p2 > p1 créé APRÈS un ticket de priorité p1
passe devant lui tant que l'écart de création reste sous (p2 - p1) × MARGE.
"""
import pytest

from app.core.config import settings
from app.core.redis import QUEUE_PRIORITY_BAND_DAYS, QUEUE_PRIORITY_BAND_MS, queue_score
from app.models.ticket import PRIORITY_SCORES, TicketPriority

# Base de temps arbitraire mais réaliste (novembre 2023, en ms).
T0 = 1_700_000_000_000


def test_la_marge_couvre_plus_que_le_ttl_des_tickets_en_attente():
    """
    Garde-fou : le TTL des tickets WAITING doit rester SOUS la marge de priorité.

    C'est ce qui rend l'invariant vrai par construction : aucun ticket ne peut
    attendre assez longtemps pour être doublé par une priorité supérieure, parce
    qu'il expire avant. Relever WAITING_TICKET_TTL_HOURS au-delà de 11,57 jours
    sans toucher à la marge casserait la priorité en silence — ce test échoue
    alors, avec le chiffre exact à corriger.
    """
    ttl_ms = settings.WAITING_TICKET_TTL_HOURS * 3_600_000
    assert ttl_ms < QUEUE_PRIORITY_BAND_MS, (
        f"WAITING_TICKET_TTL_HOURS={settings.WAITING_TICKET_TTL_HOURS} h "
        f"({ttl_ms} ms) dépasse la marge de priorité "
        f"({QUEUE_PRIORITY_BAND_MS} ms ≈ {QUEUE_PRIORITY_BAND_DAYS:.2f} jours) : "
        "un ticket ancien pourrait être doublé par une priorité supérieure. "
        "Augmenter QUEUE_PRIORITY_BAND_MS en conséquence."
    )


def test_la_marge_vaut_bien_onze_jours_et_non_une_seconde():
    """La constante représente 11,57 jours — pas 1 000 s ni 1 s (piège de lecture)."""
    assert 11 < QUEUE_PRIORITY_BAND_DAYS < 12


@pytest.mark.parametrize(
    "low,high",
    [
        (TicketPriority.STANDARD, TicketPriority.PRIORITY),
        (TicketPriority.PRIORITY, TicketPriority.VIP),
        (TicketPriority.VIP, TicketPriority.EMERGENCY),
        (TicketPriority.STANDARD, TicketPriority.EMERGENCY),
    ],
)
def test_une_priorite_superieure_double_meme_creee_plus_tard(low, high):
    """La priorité l'emporte sur l'ancienneté, dans la limite de la marge."""
    gap_ms = (PRIORITY_SCORES[high] - PRIORITY_SCORES[low]) * QUEUE_PRIORITY_BAND_MS - 1
    older = queue_score(PRIORITY_SCORES[low], T0)
    newer = queue_score(PRIORITY_SCORES[high], T0 + gap_ms)
    assert newer < older, "le ticket prioritaire doit passer devant malgré son arrivée tardive"


def test_la_priorite_ne_double_plus_au_dela_de_la_marge():
    """
    Limite documentée : au-delà de la marge, l'ancienneté reprend le dessus.

    Ce n'est pas un bug mais la borne du modèle ; elle n'est jamais atteinte
    parce que le TTL (72 h) expire bien avant (11,57 jours).
    """
    gap_ms = (PRIORITY_SCORES[TicketPriority.VIP] - PRIORITY_SCORES[TicketPriority.STANDARD]) \
        * QUEUE_PRIORITY_BAND_MS + 1
    older = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], T0)
    newer = queue_score(PRIORITY_SCORES[TicketPriority.VIP], T0 + gap_ms)
    assert newer > older


def test_fifo_a_priorite_egale():
    """À priorité égale, le premier arrivé passe le premier."""
    first = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], T0)
    second = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], T0 + 1)
    assert first < second


def test_le_score_reste_un_entier_exact_pour_redis():
    """
    Redis stocke les scores en double : au-delà de 2⁵³, l'ordre n'est plus exact.

    Le score maximal réel est (10 × 10⁹) + epoch_ms ≈ 1,7 × 10¹² : trois ordres
    de grandeur sous la limite. Ce test échoue si quelqu'un augmente la marge
    jusqu'à faire perdre la précision entière.
    """
    assert queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], int(9e12)) < 2**53
