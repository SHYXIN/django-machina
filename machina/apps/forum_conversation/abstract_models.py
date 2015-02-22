# -*- coding: utf-8 -*-

# Standard library imports
from __future__ import unicode_literals

# Third party imports
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.encoding import force_text
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import Choices

# Local application / specific library imports
from machina.core.compat import AUTH_USER_MODEL
from machina.core.compat import slugify
from machina.core.utils import refresh
from machina.models.abstract_models import DatedModel
from machina.models.fields import MarkupTextField


TOPIC_TYPES = Choices(
    (0, 'topic_post', _('Default topic')),
    (1, 'topic_sticky', _('Sticky')),
    (2, 'topic_announce', _('Announce')),
)

TOPIC_STATUSES = Choices(
    (0, 'topic_unlocked', _('Topic unlocked')),
    (1, 'topic_locked', _('Topic locked')),
    (2, 'topic_moved', _('Topic moved')),
)


@python_2_unicode_compatible
class AbstractTopic(DatedModel):
    """
    Represents a forum topic.
    """
    forum = models.ForeignKey('forum.Forum', verbose_name=_('Topic forum'), related_name='topics')
    poster = models.ForeignKey(AUTH_USER_MODEL, verbose_name=_('Poster'))

    # The subject of the thread should correspond to the one associated with the first post
    subject = models.CharField(max_length=255, verbose_name=_('Subject'))
    slug = models.SlugField(max_length=300, verbose_name=_('Slug'))

    # Sticky, Announce, Global topic or Default topic ; that's what a topic can be
    TYPE_CHOICES = TOPIC_TYPES
    type = models.PositiveSmallIntegerField(choices=TOPIC_TYPES, verbose_name=_('Topic type'), db_index=True)

    # A topic can be locked, unlocked or moved
    STATUS_CHOICES = TOPIC_STATUSES
    status = models.PositiveIntegerField(choices=TOPIC_STATUSES, verbose_name=_('Topic status'), db_index=True)

    # A topic can be approved before publishing ; defaults to True. The value of this flag
    # should correspond to the one associated with the first post
    approved = models.BooleanField(verbose_name=_('Approved'), default=True)

    # The number of posts included in this topic
    posts_count = models.PositiveIntegerField(verbose_name=_('Posts count'), editable=False, blank=True, default=0)

    # The number of time the topic has been viewed
    views_count = models.PositiveIntegerField(verbose_name=_('Views count'), editable=False, blank=True, default=0)

    # Many users can subscribe to this topic
    subscribers = models.ManyToManyField(AUTH_USER_MODEL, related_name='subscriptions', verbose_name=_('Subscribers'), blank=True, null=True)

    class Meta:
        abstract = True
        app_label = 'forum_conversation'
        ordering = ['-type', '-updated', ]
        get_latest_by = 'updated'
        verbose_name = _('Topic')
        verbose_name_plural = _('Topics')

    def __str__(self):
        if self.posts.exists():
            return '{}'.format(self.posts.all().order_by('created')[0].subject)
        else:
            return '{}'.format(self.id)

    @property
    def is_topic(self):
        return self.type == self.TYPE_CHOICES.topic_post

    @property
    def is_sticky(self):
        return self.type == self.TYPE_CHOICES.topic_sticky

    @property
    def is_announce(self):
        return self.type == self.TYPE_CHOICES.topic_announce

    @property
    def first_post(self):
        """
        Try to fetch the first post associated with the current topic and caches it to
        lighten the next request.
        """
        if not hasattr(self, '_first_post'):
            posts = self.posts.select_related('poster').all().order_by('created')
            self._first_post = posts[0] if len(posts) else None
        return self._first_post

    @property
    def last_post(self):
        """
        Try to fetch the last post associated with the current topic and caches it to
        lighten the next request.
        """
        if not hasattr(self, '_last_post'):
            posts = self.posts.select_related('poster').all().order_by('-created')
            self._last_post = posts[0] if len(posts) else None
        return self._last_post

    def clean(self):
        super(AbstractTopic, self).clean()
        if self.forum.is_category or self.forum.is_link:
            raise ValidationError(_('A topic can not be associated with a category or a link forum'))

    def save(self, *args, **kwargs):
        # It is vital to track the changes of the forum associated with a topic in order to
        # maintain counters up-to-date.
        old_instance = None
        if self.pk:
            old_instance = self.__class__._default_manager.get(pk=self.pk)

        # Update the slug field
        self.slug = slugify(force_text(self.subject))

        # Do the save
        super(AbstractTopic, self).save(*args, **kwargs)

        # If any change has been made to the parent forum, trigger the update of the counters
        if old_instance and old_instance.forum != self.forum:
            self.update_trackers()
            # The previous parent forum counters should also be updated
            if old_instance.forum:
                old_forum = refresh(old_instance.forum)
                old_forum.update_trackers()

    def _simple_save(self, *args, **kwargs):
        """
        Calls the parent save method in order to avoid the checks for topic forum changes
        which can result in triggering a new update of the counters associated with the
        current topic.
        This allow the database to not be hit by such checks during very common and regular
        operations such as those provided by the update_trackers function; indeed these operations
        will never result in an update of a topic's forum.
        This save is done without triggering the update of the 'updated' field by disabling the
        'auto_now' behavior.
        """
        self._meta.get_field_by_name('updated')[0].auto_now = False
        super(AbstractTopic, self).save(*args, **kwargs)
        self._meta.get_field_by_name('updated')[0].auto_now = True

    def delete(self, using=None):
        super(AbstractTopic, self).delete(using)
        self.forum.update_trackers()

    def update_trackers(self):
        """
        Updates the posts count, the update date and the link toward the last post
        associated with the current topic.
        """
        self.posts_count = self.posts.count()
        posts = self.posts.all().order_by('-created')
        self._last_post = posts[0] if posts.exists() else None
        self.updated = self._last_post.created
        self._simple_save()
        # Trigger the forum-level trackers update
        self.forum.update_trackers()

    def get_absolute_url(self):
        from django.core.urlresolvers import reverse
        return reverse('forum-conversation:topic', kwargs={
            'forum_slug': self.forum.slug, 'forum_pk': self.forum.pk,
            'slug': self.slug, 'pk': str(self.id)})


@python_2_unicode_compatible
class AbstractPost(DatedModel):
    """
    Represents a forum post. A forum post is always linked to a topic.
    """
    topic = models.ForeignKey('forum_conversation.Topic', verbose_name=_('Topic'), related_name='posts')
    poster = models.ForeignKey(AUTH_USER_MODEL, related_name='posts', verbose_name=_('Poster'))
    poster_ip = models.GenericIPAddressField(verbose_name=_('Poster IP address'), blank=True, null=True, default='2002::0')

    # Each post can have its own subject. The subject of the thread corresponds to the
    # one associated with the first post
    subject = models.CharField(verbose_name=_('Subject'), max_length=255)

    # Content
    content = MarkupTextField(verbose_name=_('Content'))

    # A post can be approved before publishing ; defaults to True
    approved = models.BooleanField(verbose_name=_('Approved'), default=True)

    # A post can be edited for several reason (eg. moderation) ; the reason why it has been updated can be specified
    update_reason = models.CharField(max_length=255, verbose_name=_('Update reason'), blank=True, null=True)

    # Tracking data
    updated_by = models.ForeignKey(AUTH_USER_MODEL, verbose_name=_('Lastly updated by'), editable=False, blank=True, null=True)
    updates_count = models.PositiveIntegerField(verbose_name=_('Updates count'), editable=False, blank=True, default=0)

    class Meta:
        abstract = True
        app_label = 'forum_conversation'
        ordering = ['created', ]
        get_latest_by = 'created'
        verbose_name = _('Post')
        verbose_name_plural = _('Posts')

    def __str__(self):
        return '{}'.format(self.subject)

    @property
    def is_topic_head(self):
        return self.topic.first_post.id == self.id

    @property
    def is_topic_tail(self):
        return self.topic.last_post.id == self.id

    @property
    def position(self):
        position = self.topic.posts.filter(Q(created__lt=self.created) | Q(id=self.id)).count()
        return position

    def save(self, *args, **kwargs):
        super(AbstractPost, self).save(*args, **kwargs)

        # Ensures that the subject of the thread corresponds to the one associated
        # with the first post. Do the same with the 'approved' flag.
        if self.is_topic_head:
            if self.subject != self.topic.subject or self.approved != self.topic.approved:
                self.topic.subject = self.subject
                self.topic.approved = self.approved

        # Trigger the topic-level trackers update
        self.topic.update_trackers()

    def delete(self, using=None):
        if self.is_topic_head and self.is_topic_tail:
            # The default way of operating is to trigger the deletion of the associated topic
            # only if the considered post is the only post embedded in the topic
            self.topic.delete()
        else:
            super(AbstractPost, self).delete(using)
            self.topic.update_trackers()