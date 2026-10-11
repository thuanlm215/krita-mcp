// SPDX-License-Identifier: GPL-2.0-or-later
// Small ABI-pinned bridge to Krita 6.0.4's KUndo2 stack and painting pipeline.
#include <kundo2stack.h>
#include <QPointer>
#include <QThread>
#include <QCoreApplication>
#include <QLineF>
#include <kis_figure_painting_tool_helper.h>
#include <kis_node.h>
#include <kis_image.h>
#include <kis_raster_keyframe_channel.h>
#include <kis_paint_layer.h>
#include <kis_paint_device.h>
#include <kis_post_execution_undo_adapter.h>
#include <kis_image_animation_interface.h>
#include <kis_transaction.h>
#include <commands/kis_selection_commands.h>
#include <KisSynchronizedConnection.h>
#include <atomic>
#include <QJsonArray>
#include <QJsonObject>
#include <QJsonDocument>
#include <typeinfo>

class Node { public: KisNodeSP node() const; };
class Selection { public: KisSelectionSP selection() const; };

// Exported out-of-line getters. We neither instantiate these classes nor
// inspect their layouts; the actual receivers belong to Krita's Qt window.
class KisDocument { public: KUndo2Stack *undoStack(); };
class KisCanvasResourceProvider { public: KoCanvasResourceProvider *resourceManager(); };
class KisViewManager { public: KisCanvasResourceProvider *canvasResourceProvider(); };
class KisView { public: KisDocument *document() const; KisViewManager *viewManager() const; };
class KisMainWindow { public: KisView *activeView() const; };

class CallbackCommand final : public KUndo2Command {
    QPointer<QObject> callback;
    void invoke(const char *slot) {
        if (callback && callback->thread() == QThread::currentThread())
            QMetaObject::invokeMethod(callback, slot, Qt::DirectConnection);
    }
public:
    CallbackCommand(QObject *cb, const char *label)
        : KUndo2Command(kundo2_noi18n(QString::fromUtf8(label))), callback(cb) {}
    ~CallbackCommand() override { invoke("dispose"); }
    void undo() override { invoke("undo"); }
    void redo() override { invoke("redo"); }
};

static std::atomic<qint64> pixelHistoryBytes{0};
class PixelCommand final : public KUndo2Command {
    KisNodeSP node;
    KisPaintDeviceSP device;
    QRect rect;
    QByteArray before,after;
    KUndo2CommandSP transaction;
public:
    PixelCommand(KisNodeSP n,KisPaintDeviceSP d,QRect r,QByteArray b,QByteArray a,const char *label)
        : KUndo2Command(kundo2_noi18n(QString::fromUtf8(label))),node(n),device(d),rect(r),before(b),after(a) {
        pixelHistoryBytes+=before.size()+after.size();
        // Participate in the device memento chain. Untracked writeBytes in
        // undo/redo corrupts the ordering of a preceding native brush stroke.
        KisTransaction edit(kundo2_noi18n(QString::fromUtf8(label)),device);
        device->writeBytes(reinterpret_cast<const quint8*>(after.constData()),
                           rect.x(),rect.y(),rect.width(),rect.height());
        transaction=KUndo2CommandSP(edit.endAndTake());
        node->setDirty(rect);
    }
    ~PixelCommand() override { pixelHistoryBytes-=before.size()+after.size(); }
    void undo() override {transaction->undo();node->setDirty(rect);}
    void redo() override {transaction->redo();node->setDirty(rect);}
};
class VisibilityCommand final : public KUndo2Command {
    KisNodeSP node;
    bool before,after;
public:
    VisibilityCommand(KisNodeSP n,bool a):KUndo2Command(kundo2_noi18n("MCP Layer Visibility")),
        node(n),before(n->visible()),after(a) {}
    void undo() override {node->setVisible(before);node->setDirty();}
    void redo() override {node->setVisible(after);node->setDirty();}
};

struct StagedPixels {
    KisImageSP image;
    KisNodeSP node;
    KisPaintDeviceSP source,preview;
    QRect rect;
    QByteArray before,after,label;
};
struct StagedFrame {
    KisImageSP image;
    KisNodeSP node;
    KisPaintDeviceSP before,after;
    int time,policy;
    qint64 budget;
    KUndo2QStack *stack;
};
class FrameCommand final : public KUndo2Command {
    qint64 budget;
public:
    FrameCommand(qint64 b):KUndo2Command(kundo2_noi18n("MCP exact frame repair")),budget(b) {pixelHistoryBytes+=budget;}
    ~FrameCommand() override {pixelHistoryBytes-=budget;}
};

extern "C" {
void *mcp_stack(void *window);
const char *mcp_native_abi() { return "krita-6.0.4-qt6"; }
int mcp_selection(void *window,void *wrapper,void *selection) {
    if (!mcp_stack(window) || !wrapper) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return 0;
    auto image=node->image();image->barrierLock();
    auto value=selection ? static_cast<Selection*>(selection)->selection() : KisSelectionSP();
    auto command=KUndo2CommandSP(new KisSetGlobalSelectionCommand(image,value));
    command->redo();
    // Like cut/paste, Undo/Redo must execute on the image scheduler. A direct
    // legacy selection command inside a GUI macro can run BEFORE queued paste
    // Undo restores its mask; Redo would then add an already parented mask.
    image->postExecutionUndoAdapter()->addCommand(command);
    image->unlock();return 1;
}
int mcp_image_lock(void *wrapper,int lock) {
    if (!wrapper) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return 0;
    if (lock) node->image()->barrierLock(true);
    else node->image()->unlock();
    return 1;
}
const char *mcp_history_details(void *stack) {
    static QByteArray output;
    auto *s=static_cast<KUndo2QStack*>(stack);
    if (!s) return nullptr;
    std::function<QJsonObject(const KUndo2Command*)> describe=[&](const KUndo2Command *c) {
        QJsonArray children;
        for (int i=0;i<c->childCount();++i) children.append(describe(c->child(i)));
        return QJsonObject{{"type",QString::fromLatin1(typeid(*c).name())},
                           {"label",c->actionText()},{"children",children}};
    };
    QJsonArray commands;
    for (int i=0;i<s->count();++i) commands.append(describe(s->command(i)));
    output=QJsonDocument(commands).toJson(QJsonDocument::Compact);return output.constData();
}
void mcp_flush_events() { KisSynchronizedConnectionBase::forceDeliverAllSynchronizedEvents(); }
int mcp_ui_time(void *wrapper) {
    if (!wrapper) return -1;
    auto node=static_cast<Node*>(wrapper)->node();
    return node && node->image() ? node->image()->animationInterface()->currentUITime() : -1;
}
void *mcp_stack(void *window) {
    auto *obj = static_cast<QObject*>(window);
    if (!obj || !obj->inherits("KisMainWindow") || obj->thread()!=QThread::currentThread())
        return nullptr;
    auto *view = static_cast<KisMainWindow*>(window)->activeView();
    auto *document = view ? view->document() : nullptr;
    return document ? document->undoStack() : nullptr;
}
int mcp_index(void *stack) { return stack ? static_cast<KUndo2QStack*>(stack)->index() : -1; }
int mcp_count(void *stack) { return stack ? static_cast<KUndo2QStack*>(stack)->count() : -1; }
const char *mcp_history_label(void *stack,int index) {
    static QByteArray label;
    auto *s=static_cast<KUndo2QStack*>(stack);
    if (!s || index<0 || index>=s->count()) return nullptr;
    label=s->command(index)->actionText().toUtf8();return label.constData();
}
int mcp_time(void *window,void *wrapper,int time) {
    if (!mcp_stack(window) || !wrapper || time<0) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return 0;
    // libkis Document::setCurrentTime calls requestTimeSwitchWithUndo.
    // Inspection/scrubbing must not append a Switch Current Time command or
    // destroy the redo branch. Use the same scheduler with useUndo=false.
    node->image()->animationInterface()->requestTimeSwitchNonGUI(time,false);
    return 1;
}
void mcp_begin(void *stack, const char *label) {
    if (stack) static_cast<KUndo2QStack*>(stack)->beginMacro(kundo2_noi18n(QString::fromUtf8(label)));
}
void mcp_end(void *stack) { if (stack) static_cast<KUndo2QStack*>(stack)->endMacro(); }
void mcp_purge_redo(void *stack) { if (stack) static_cast<KUndo2QStack*>(stack)->purgeRedoState(); }
void mcp_push(void *stack, void *callback, const char *label) {
    if (stack && callback) static_cast<KUndo2QStack*>(stack)->push(new CallbackCommand(static_cast<QObject*>(callback),label));
}
int mcp_push_state(void *window,void *wrapper,void *callback,const char *label) {
    auto *stack=static_cast<KUndo2QStack*>(mcp_stack(window));
    if (!stack || !wrapper || !callback) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return 0;
    // Synchronize direct GUI metadata pushes with image-worker history pushes.
    // Callback restore must never synchronously render while this lock is held.
    auto image=node->image();image->barrierLock();
    stack->push(new CallbackCommand(static_cast<QObject*>(callback),label));
    image->unlock();return 1;
}
int mcp_pixels(void *window,void *wrapper,int x,int y,int w,int h,const char *data,int length,const char *label) {
    if (!mcp_stack(window) || !wrapper || !data || w<=0 || h<=0 || qint64(w)*h>16777216) return -1;
    auto node=static_cast<Node*>(wrapper)->node();
    auto device=node ? node->paintDevice() : KisPaintDeviceSP();
    if (!device || !node->image() || qint64(w)*h*device->pixelSize()!=length) return -1;
    if (pixelHistoryBytes.load()+qint64(length)*2>268435456) return -2;
    auto image=node->image();image->barrierLock();
    QByteArray before(length,'\0'),after(data,length);
    device->readBytes(reinterpret_cast<quint8*>(before.data()),x,y,w,h);
    if (before==after) {image->unlock();return 0;}
    auto command=KUndo2CommandSP(new PixelCommand(node,device,QRect(x,y,w,h),before,after,label));
    command->redo();
    image->postExecutionUndoAdapter()->addCommand(command);
    image->unlock();return 1;
}
void *mcp_stage_pixels(void *window,void *wrapper,int x,int y,int w,int h,const char *data,int length,const char *label,int *status) {
    if (status) *status=-1;
    if (!status || !mcp_stack(window) || !wrapper || !data || w<=0 || h<=0 || qint64(w)*h>1048576) return nullptr;
    auto node=static_cast<Node*>(wrapper)->node();
    auto device=node ? node->paintDevice() : KisPaintDeviceSP();
    if (!device || !node->image() || qint64(w)*h*device->pixelSize()!=length) return nullptr;
    if (pixelHistoryBytes.load()+qint64(length)*2>268435456) {*status=-2;return nullptr;}
    auto image=node->image();image->barrierLock();
    QByteArray before(length,'\0'),after(data,length);
    device->readBytes(reinterpret_cast<quint8*>(before.data()),x,y,w,h);
    if (before==after) {image->unlock();*status=0;return nullptr;}
    // Even an aborted source memento can invalidate older native redo tiles.
    // Mutate only an owned snapshot before publication, while retaining the
    // source barrier. Starting its real transaction is the final commit step.
    auto preview=KisPaintDeviceSP(new KisPaintDevice(*device));
    preview->writeBytes(reinterpret_cast<const quint8*>(after.constData()),x,y,w,h);
    *status=1;
    // No history push yet: callers can abort a provisional write and retain
    // the complete preexisting redo branch. No event pumping until finish.
    return new StagedPixels{image,node,device,preview,QRect(x,y,w,h),before,after,QByteArray(label)};
}
void mcp_finish_pixels(void *stage,int commit) {
    auto *edit=static_cast<StagedPixels*>(stage);
    if (!edit) return;
    if (commit) {
        auto command=KUndo2CommandSP(new PixelCommand(edit->node,edit->source,edit->rect,
                                                    edit->before,edit->after,edit->label.constData()));
        command->redo();
        edit->image->postExecutionUndoAdapter()->addCommand(command);
    }
    edit->image->unlock();
    delete edit;
}
void *mcp_stage_frame(void *window,void *wrapper,int time,int policy,int x,int y,int w,int h,
                     const char *data,int length,int *status) {
    if (status) *status=-1;
    if (!status || !mcp_stack(window) || !wrapper || !data || time<0 || time>=100000 ||
        (policy<0 || policy>2) || w<=0 || h<=0 || qint64(w)*h>1048576 || qint64(w)*h*4!=length) return nullptr;
    auto node=static_cast<Node*>(wrapper)->node();
    auto device=node ? node->paintDevice() : KisPaintDeviceSP();
    auto *channel=node ? dynamic_cast<KisRasterKeyframeChannel*>(node->getKeyframeChannel(KisKeyframeChannel::Raster.id())) : nullptr;
    if (!device || !node->image() || !channel || device->pixelSize()!=4) return nullptr;
    auto key=channel->activeKeyframeAt<KisRasterKeyframe>(time);
    if (!key) return nullptr;
    auto image=node->image();image->barrierLock();
    auto before=KisPaintDeviceSP(new KisPaintDevice(device->colorSpace()));
    key->writeFrameToDevice(before); // Exact held frame, including off-canvas.
    auto after=KisPaintDeviceSP(new KisPaintDevice(*before));
    QByteArray old(length,'\0');before->readBytes(reinterpret_cast<quint8*>(old.data()),x,y,w,h);
    if (policy!=2 && old==QByteArray(data,length)) {image->unlock();*status=0;return nullptr;}
    after->writeBytes(reinterpret_cast<const quint8*>(data),x,y,w,h);
    QRect extent=before->exactBounds().united(after->exactBounds());
    qint64 budget=qMax(qint64(length),qint64(extent.width())*extent.height()*4)*3;
    if (pixelHistoryBytes.load()+budget>268435456) {image->unlock();*status=-2;return nullptr;}
    *status=1;
    return new StagedFrame{image,node,before,after,time,policy,budget,static_cast<KUndo2QStack*>(mcp_stack(window))};
}
int mcp_finish_frame(void *stage,int commit,int failAfterFirstKey) {
    auto *edit=static_cast<StagedFrame*>(stage);
    if (!edit) return 0;
    int result=0;
    if (commit) {
        auto *channel=dynamic_cast<KisRasterKeyframeChannel*>(edit->node->getKeyframeChannel(KisKeyframeChannel::Raster.id()));
        auto command=KUndo2CommandSP(new FrameCommand(edit->budget));
        if (edit->policy==1 && !channel->keyframeAt(edit->time+1))
            channel->importFrame(edit->time+1,edit->before,command.data());
        if (failAfterFirstKey) {
            // Fixed private test fault after a topology mutation, before any
            // history publication. Consume SkipFirstRedo then undo OWN child
            // commands; preexisting source frame and redo tiles stay intact.
            command->redo();command->undo();result=-3;
        } else {
            int target=edit->policy!=0 ? edit->time : channel->activeKeyframeTime(edit->time);
            channel->importFrame(target,edit->after,command.data());
            command->redo();
            // Opening a macro truncates redo. Do so ONLY once staging and
            // rollback-capable topology work succeeded, at publication.
            edit->stack->beginMacro(kundo2_noi18n("MCP exact frame repair"));
            edit->image->postExecutionUndoAdapter()->addCommand(command);
            result=1;
        }
        edit->node->setDirty();
    }
    edit->image->unlock();delete edit;
    return result;
}
int mcp_visible(void *window,void *wrapper,int value) {
    if (!mcp_stack(window) || !wrapper) return -1;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return -1;
    if (node->visible()==(value!=0)) return 0;
    auto image=node->image();image->barrierLock();
    auto command=KUndo2CommandSP(new VisibilityCommand(node,value!=0));
    command->redo();image->postExecutionUndoAdapter()->addCommand(command);
    image->unlock();return 1;
}
int mcp_projection_bounds(void *wrapper,int *values) {
    if (!wrapper || !values) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    auto projection=node ? node->projection() : KisPaintDeviceSP();
    if (!projection) return 0;
    QRect r=projection->exactBounds();
    values[0]=r.x();values[1]=r.y();values[2]=r.width();values[3]=r.height();return 1;
}
int mcp_stroke(void *window, void *wrapper, const double *x, const double *y, const double *pressure,
               const int *tx, const int *ty, const double *times, int count) {
    if (!wrapper || count<2 || !mcp_stack(window)) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return 0;
    // Same native painting helper used by libkis Node.paintLine/paintPath,
    // kept alive across every sample, with one native stroke/Undo entry.
    auto *manager=static_cast<KisMainWindow*>(window)->activeView()->viewManager();
    if (!manager || !manager->canvasResourceProvider()) return 0;
    auto *resources=manager->canvasResourceProvider()->resourceManager();
    if (!resources) return 0;
    KisFigurePaintingToolHelper helper(kundo2_noi18n("MCP Brush Stroke"),node->image(),node,
        resources,KisToolShapeUtils::StrokeStyleForeground,KisToolShapeUtils::FillStyleNone);
    KisPaintInformation previous(QPointF(x[0],y[0]),pressure[0],tx[0],ty[0],0,0,1,times[0],0);
    for (int i=1;i<count;++i) {
        double dt=qMax(1.0,times[i]-times[i-1]);
        double speed=QLineF(QPointF(x[i-1],y[i-1]),QPointF(x[i],y[i])).length()/dt;
        KisPaintInformation current(QPointF(x[i],y[i]),pressure[i],tx[i],ty[i],0,0,1,times[i],speed);
        helper.paintLine(previous,current);
        previous=current;
    }
    return 1;
}
int mcp_frames(void *wrapper, int *times, int capacity) {
    if (!wrapper) return -1;
    auto node=static_cast<Node*>(wrapper)->node();
    auto *channel=node ? node->getKeyframeChannel(KisKeyframeChannel::Raster.id()) : nullptr;
    if (!channel) return 0;
    auto keys=channel->allKeyframeTimes().values();
    std::sort(keys.begin(),keys.end());
    for (int i=0;i<qMin(capacity,keys.size());++i) times[i]=keys[i];
    return keys.size();
}
int mcp_frame(void *window, void *wrapper, int time, int mode, int source) {
    auto *stack=static_cast<KUndo2QStack*>(mcp_stack(window));
    if (!wrapper || !stack || time<0 || mode<0 || mode>2) return 0;
    auto node=static_cast<Node*>(wrapper)->node();
    if (!node || !node->image()) return 0;
    auto *channel=node->getKeyframeChannel(KisKeyframeChannel::Raster.id());
    if (!channel || (mode==2 && (time==0 || !channel->keyframeAt(time))) ||
        (mode==1 && (!channel->keyframeAt(source) || source==time))) return 0;
    auto image=node->image();
    image->barrierLock();
    auto *command=new KUndo2Command(kundo2_noi18n("MCP Animation Frame"));
    if (mode==0) channel->addKeyframe(time,command);
    else if (mode==1) channel->copyKeyframe(source,time,command);
    else channel->removeKeyframe(time,command);
    // Keyframe methods install SkipFirstRedo child commands. push() executes
    // their first redo once; later undo/redo is handled by Krita's history.
    stack->push(command);
    image->unlock();
    return 1;
}
int mcp_onion(void *wrapper, int value) {
    if (!wrapper) return -1;
    auto node=static_cast<Node*>(wrapper)->node();
    auto *layer=dynamic_cast<KisPaintLayer*>(node.data());
    if (!layer) return -1;
    if (value>=0) layer->setOnionSkinEnabled(value!=0);
    return layer->onionSkinEnabled() ? 1 : 0;
}
}
