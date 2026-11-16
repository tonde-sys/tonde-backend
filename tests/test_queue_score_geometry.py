"""
Tests — géométrie du score de file (constat P2-16 de l'audit).

Le score Redis d'un ticket vaut `(10 - priorité) × MARGE + SÉQUENCE DE FILE`.
La MARGE vaut 10⁹. Depuis la migration 013 la partie basse est une séquence
monotone et non plus un horodatage : ce fichier verrouille donc l'INVARIANT
(la priorité l'emporte sur l'ordre d'arrivée dans la limite de la marge), pas la
constante. Un futur refactor qui remplacerait 10⁹ par 1 000 casserait la priorité
sans qu'aucun test métier ne s'en aperçoive — c'est précisément ce que ces tests
empêchent.

Invariant : un ticket de priorité p2 > p1 entré APRÈS un ticket de priorité p1
passe devant lui tant que l'écart de séquence reste sous (p2 - p1) × MARGE.
"""
import pytest

from app.core.config import settings
from app.core.redis import QUEUE_PRIORITY_BAND, queue_score
from app.models.ticket import PRIORITY_SCORES, TicketPriority

# Séquence de départ arbitraire mais réaliste (une agence qui tourne depuis des
# mois). Ce n'est plus un horodatage : seule sa CROISSANCE compte.
S0 = 1_000_000

# Débit maximal plausible de tout le système, en tickets par heure. Sert à
# démontrer que l'étendue des séquences dans une fenêtre de rétention reste très
# en dessous de la marge. Volontairement généreux : 10 000 tickets/heure, soit
# ~2,4 tickets par seconde en permanence.
MAX_TICKETS_PER_HOUR = 10_000


def test_la_marge_couvre_l_etendue_des_sequences_sur_la_duree_de_retention():
    """
    Garde-fou : sur toute la durée de rétention, les séquences restent sous la marge.

    C'est ce qui rend l'invariant vrai par construction : aucun ticket ne peut
    être assez ancien pour être doublé par une priorité supérieure, parce qu'il
    expire avant que l'étendue des séquences n'atteigne la marge. Relever
    WAITING_TICKET_TTL_HOURS, ou diviser la marge, casse la priorité en silence —
    ce test échoue alors avec les chiffres exacts à corriger.
    """
    etendue = settings.WAITING_TICKET_TTL_HOURS * MAX_TICKETS_PER_HOUR
    assert etendue < QUEUE_PRIORITY_BAND, (
        f"Sur {settings.WAITING_TICKET_TTL_HOURS} h à {MAX_TICKETS_PER_HOUR} tickets/h, "
        f"l'étendue des séquences atteint {etendue}, au-delà de la marge "
        f"({QUEUE_PRIORITY_BAND}) : un ticket ancien pourrait être doublé par une "
        "priorité supérieure. Augmenter QUEUE_PRIORITY_BAND en conséquence."
    )


def test_la_marge_vaut_un_milliard_et_non_mille():
    """La constante couvre un milliard de tickets — pas 1 000 (piège de lecture)."""
    assert QUEUE_PRIORITY_BAND >= 1_000_000_000


@pytest.mark.parametrize(
    "low,high",
    [
        (TicketPriority.STANDARD, TicketPriority.PRIORITY),
        (TicketPriority.PRIORITY, TicketPriority.VIP),
        (TicketPriority.VIP, TicketPriority.EMERGENCY),
        (TicketPriority.STANDARD, TicketPriority.EMERGENCY),
    ],
)
def test_une_priorite_superieure_passe_toujours_devant(low, high):
    """
    La priorité l'emporte INCONDITIONNELLEMENT — propriété plus forte qu'avant.

    Du temps de l'horodatage, l'invariant n'était vrai que si l'écart de création
    restait sous la marge (11,57 jours). La séquence étant désormais bornée par
    la marge elle-même, l'écart maximal possible est plus petit que la marge : un
    ticket prioritaire passe donc devant le ticket le plus ancien possible, même
    s'il est le plus récent possible.
    """
    plus_ancien = queue_score(PRIORITY_SCORES[low], 0)
    plus_recent = queue_score(PRIORITY_SCORES[high], QUEUE_PRIORITY_BAND - 1)
    assert plus_recent < plus_ancien


def test_fifo_a_priorite_egale():
    """À priorité égale, la séquence la plus petite passe devant."""
    first = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], S0)
    second = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], S0 + 1)
    assert first < second


def test_deux_tickets_de_la_meme_milliseconde_ne_sont_plus_egaux():
    """
    Le défaut corrigé par la migration 013, énoncé comme un test.

    Avec un horodatage en partie basse, deux tickets créés dans la même
    milliseconde avaient le MÊME score : Redis les départageait alors par ordre
    lexicographique de leur UUID. Deux séquences distinctes ne peuvent pas être
    égales — il n'y a plus de cas d'égalité à trancher.
    """
    a = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], S0)
    b = queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], S0 + 1)
    assert a != b


def test_le_score_reste_un_entier_exact_pour_redis():
    """
    Redis stocke les scores en double : au-delà de 2⁵³, l'ordre n'est plus exact.

    Le score maximal réel est (10 × 10⁹) + séquence ≈ 1,1 × 10¹⁰ : six ordres de
    grandeur sous la limite. Ce test échoue si quelqu'un augmente la marge
    jusqu'à faire perdre la précision entière.
    """
    assert queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], QUEUE_PRIORITY_BAND - 1) < 2**53


def test_une_sequence_hors_marge_est_refusee():
    """
    Une séquence au-delà de la marge est une ERREUR, pas un ordre approximatif.

    Mieux vaut une exception bruyante qu'une file silencieusement mal ordonnée :
    une séquence négative ou démesurée signale un bug d'attribution, et la
    masquer ferait passer des VIP derrière des standards sans trace.
    """
    with pytest.raises(ValueError):
        queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], QUEUE_PRIORITY_BAND)
    with pytest.raises(ValueError):
        queue_score(PRIORITY_SCORES[TicketPriority.STANDARD], -1)
