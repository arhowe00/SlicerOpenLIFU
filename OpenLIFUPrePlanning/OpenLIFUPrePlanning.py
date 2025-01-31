from typing import Optional, TYPE_CHECKING, Dict, List
from functools import partial
from collections import defaultdict

import qt
import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper
from slicer import vtkMRMLMarkupsFiducialNode, vtkMRMLScalarVolumeNode

from OpenLIFULib import (
    get_target_candidates,
    get_openlifu_data_parameter_node,
    OpenLIFUAlgorithmInputWidget,
    SlicerOpenLIFUProtocol,
    SlicerOpenLIFUTransducer,
)
from OpenLIFULib.util import replace_widget

if TYPE_CHECKING:
    from OpenLIFUData.OpenLIFUData import OpenLIFUDataLogic

PLACE_INTERACTION_MODE_ENUM_VALUE = slicer.vtkMRMLInteractionNode().Place

class OpenLIFUPrePlanning(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("OpenLIFU Pre-Planning")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "OpenLIFU.OpenLIFU Modules")]
        self.parent.dependencies = []  # add here list of module names that this module requires
        self.parent.contributors = ["Ebrahim Ebrahim (Kitware), Sadhana Ravikumar (Kitware), Peter Hollender (Openwater), Sam Horvath (Kitware), Brad Moore (Kitware)"]
        # short description of the module and a link to online module documentation
        # _() function marks text as translatable to other languages
        self.parent.helpText = _(
            "This is the pre-planning module of the OpenLIFU extension for focused ultrasound. "
            "More information at <a href=\"https://github.com/OpenwaterHealth/SlicerOpenLIFU\">github.com/OpenwaterHealth/SlicerOpenLIFU</a>."
        )
        # organization, grant, and thanks
        self.parent.acknowledgementText = _(
            "This is part of Openwater's OpenLIFU, an open-source "
            "hardware and software platform for Low Intensity Focused Ultrasound (LIFU) research "
            "and development."
        )



#
# OpenLIFUPrePlanningParameterNode
#


@parameterNodeWrapper
class OpenLIFUPrePlanningParameterNode:
    """
    The parameters needed by module.

    """


#
# OpenLIFUPrePlanningWidget
#


class OpenLIFUPrePlanningWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        self.node_observations : Dict[str:List[int]] = defaultdict(list)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        uiWidget = slicer.util.loadUI(self.resourcePath("UI/OpenLIFUPrePlanning.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)


        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        # Create logic class. Logic implements all computations that should be possible to run
        # in batch mode, without a graphical user interface.
        self.logic = OpenLIFUPrePlanningLogic()

        # Prevents possible creation of two OpenLIFUData widgets
        # see https://github.com/OpenwaterHealth/SlicerOpenLIFU/issues/120
        slicer.util.getModule("OpenLIFUData").widgetRepresentation()

        # These connections ensure that we update parameter node when scene is closed
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        # Make sure parameter node is initialized (needed for module reload)
        self.initializeParameterNode()

        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeAddedEvent, self.onNodeAdded)
        self.addObserver(slicer.mrmlScene, slicer.vtkMRMLScene.NodeRemovedEvent, self.onNodeRemoved)
        self.addObserver(get_openlifu_data_parameter_node().parameterNode, vtk.vtkCommand.ModifiedEvent, self.onDataParameterNodeModified)

        # Replace the placeholder algorithm input widget by the actual one
        algorithm_input_names = ["Protocol", "Transducer", "Volume", "Target"]
        self.algorithm_input_widget = OpenLIFUAlgorithmInputWidget(algorithm_input_names, parent = self.ui.algorithmInputWidgetPlaceholder.parentWidget())
        replace_widget(self.ui.algorithmInputWidgetPlaceholder, self.algorithm_input_widget, self.ui)

        self.ui.targetListWidget.currentItemChanged.connect(self.onTargetListWidgetCurrentItemChanged)

        position_coordinate_validator = qt.QDoubleValidator(slicer.util.mainWindow())
        position_coordinate_validator.setNotation(qt.QDoubleValidator.StandardNotation)
        self.targetPositionInputs = [
            self.ui.positionRLineEdit,
            self.ui.positionALineEdit,
            self.ui.positionSLineEdit,
        ]
        for positionLineEdit in self.targetPositionInputs:
            positionLineEdit.setValidator(position_coordinate_validator)
            positionLineEdit.editingFinished.connect(self.onTargetPositionEditingFinished)

        # Watch any fiducial nodes that already existed before this module was set up
        for fiducial_node in slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode"):
            self.watch_fiducial_node(fiducial_node)

        self.updateTargetsListView()
        self.updateApproveButtonEnabled()
        self.updateInputOptions()
        self.updateApprovalStatusLabel()
        self.updateEditTargetEnabled()
        self.updateTargetPositionInputs()
        self.updateLockButtonIcon()

        self.ui.newTargetButton.clicked.connect(self.onNewTargetClicked)
        self.ui.removeTargetButton.clicked.connect(self.onremoveTargetClicked)
        self.ui.lockButton.clicked.connect(self.onLockClicked)
        self.ui.approveButton.clicked.connect(self.onApproveClicked)
        self.ui.virtualfitButton.clicked.connect(self.onvirtualfitClicked)

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def enter(self) -> None:
        """Called each time the user opens this module."""
        # Make sure parameter node exists and observed
        self.initializeParameterNode()

    def exit(self) -> None:
        """Called each time the user opens a different module."""
        # Do not react to parameter node changes (GUI will be updated when the user enters into the module)
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event) -> None:
        """Called just before the scene is closed."""
        # Parameter node will be reset, do not use it anymore
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        """Called just after the scene is closed."""
        # If this module is shown while the scene is closed then recreate a new parameter node immediately
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and observed."""
        # Parameter node stores all user choices in parameter values, node selections, etc.
        # so that when the scene is saved and reloaded, these settings are restored.

        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode: Optional[OpenLIFUPrePlanningParameterNode]) -> None:
        """
        Set and observe parameter node.
        Observation is needed because when the parameter node is changed then the GUI must be updated immediately.
        """

        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)

        self._parameterNode = inputParameterNode
        if self._parameterNode:
            # Note: in the .ui file, a Qt dynamic property called "SlicerParameterName" is set on each
            # ui element that needs connection.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.watch_fiducial_node(node)

        self.updateTargetsListView()
        self.updateInputOptions()

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeRemoved(self, caller, event, node : slicer.vtkMRMLNode) -> None:
        if node.IsA('vtkMRMLMarkupsFiducialNode'):
            self.unwatch_fiducial_node(node)
            self.logic.revoke_approval_if_any(node)
        self.updateTargetsListView()
        self.updateInputOptions()

    def watch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Add observers so that point-list changes in this fiducial node are tracked by the module."""
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAddedEvent,partial(self.onPointAddedOrRemoved, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointRemovedEvent,partial(self.onPointAddedOrRemoved, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointModifiedEvent,partial(self.onPointModified, node)))
        self.node_observations[node.GetID()].append(node.AddObserver(slicer.vtkMRMLMarkupsNode.LockModifiedEvent,self.onLockModified))

    def unwatch_fiducial_node(self, node:vtkMRMLMarkupsFiducialNode):
        """Un-does watch_fiducial_node; see watch_fiducial_node."""
        for tag in self.node_observations.pop(node.GetID()):
            node.RemoveObserver(tag)

    def onPointAddedOrRemoved(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetsListView()
        self.updateInputOptions()
        self.logic.revoke_approval_if_any(node)

    def onPointModified(self, node:vtkMRMLMarkupsFiducialNode, caller, event):
        self.updateTargetPositionInputs()
        self.logic.revoke_approval_if_any(node)

    def onLockModified(self, caller, event):
        self.updateLockButtonIcon()
        self.updateEditTargetEnabled()

    def updateTargetsListView(self):
        """Update the list of targets in the target management UI"""
        self.ui.targetListWidget.clear()
        for target_node in get_target_candidates():
            item = qt.QListWidgetItem(target_node.GetName())
            item.setData(qt.Qt.UserRole, target_node)
            self.ui.targetListWidget.addItem(item)

    def getTargetsListViewCurrentSelection(self) -> Optional[vtkMRMLMarkupsFiducialNode]:
        """Get the fiducial node associated to the currently selected target in the list view;
        returns None if nothing is selected."""
        item = self.ui.targetListWidget.currentItem()
        if item is None:
            return None
        return item.data(qt.Qt.UserRole)

    def selectTargetByID(self, fiducial_node_mrml_id:str):
        """Set the currently selected target in the targets list widget to the one with the given ID, if it is there.
        If it is not there then then the selection is unaffected."""
        for i in range(self.ui.targetListWidget.count):
            item = self.ui.targetListWidget.item(i)
            if item.data(qt.Qt.UserRole).GetID() == fiducial_node_mrml_id:
                self.ui.targetListWidget.setCurrentItem(item)
                break

    def onTargetListWidgetCurrentItemChanged(self, current:qt.QListWidgetItem, previous:qt.QListWidgetItem):
        self.updateEditTargetEnabled()
        self.updateTargetPositionInputs()
        self.updateLockButtonIcon()

    def onDataParameterNodeModified(self,caller, event) -> None:
        self.updateApproveButtonEnabled()
        self.updateInputOptions()
        self.updateApprovalStatusLabel()

    def updateEditTargetEnabled(self):
        """Update whether the controls that edit targets are enabled"""
        current_selection = self.getTargetsListViewCurrentSelection()
        target_position_inputs_enabled = (current_selection is not None) and (not current_selection.GetLocked())
        target_deletion_and_locking_enabled = current_selection is not None
        for widget in self.targetPositionInputs:
            widget.setEnabled(target_position_inputs_enabled)
        for widget in [self.ui.removeTargetButton, self.ui.lockButton]:
            widget.setEnabled(target_deletion_and_locking_enabled)

    def onNewTargetClicked(self):
        # If we are already in point placement mode then do nothing
        if slicer.mrmlScene.GetNodeByID("vtkMRMLInteractionNodeSingleton").GetCurrentInteractionMode() == PLACE_INTERACTION_MODE_ENUM_VALUE:
            return

        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode")
        node.SetMaximumNumberOfControlPoints(1)
        node.SetName(slicer.mrmlScene.GenerateUniqueName("Target"))
        node.SetMarkupLabelFormat("%N")

        slicer.modules.markups.logic().StartPlaceMode(
            False # "place mode persistence" set to False means we want to place one target and then stop
        )

    def onremoveTargetClicked(self):
        node = self.getTargetsListViewCurrentSelection()
        if node is None:
            raise RuntimeError("It should not be possible to click Remove target while there is not a valid target selected.")
        slicer.mrmlScene.RemoveNode(node)

    def updateTargetPositionInputs(self):
        node = self.getTargetsListViewCurrentSelection()

        if node is None:
            for positionLineEdit in self.targetPositionInputs:
                positionLineEdit.text = ""
            return

        position_ras = node.GetNthControlPointPosition(0)
        for coord_value, positionLineEdit in zip(position_ras,self.targetPositionInputs):
            if not positionLineEdit.hasFocus():
                # If the RAS coordinates are not being input by the user, round what is displayed for easier reading.
                # Note that this only affects what is displayed and isn't actually rounding the position of the point.
                coord_value = f"{coord_value:0.2f}"

            positionLineEdit.text = coord_value

    def onTargetPositionEditingFinished(self):
        try:
            new_ras_position = [float(positionLineEdit.text) for positionLineEdit in self.targetPositionInputs]
        except ValueError: # The text was not convertible float (e.g blank input)
            return
        node = self.getTargetsListViewCurrentSelection()
        node.SetNthControlPointPosition(0,*new_ras_position)

    def updateLockButtonIcon(self):
        node = self.getTargetsListViewCurrentSelection()
        if node is None:
            self.ui.lockButton.setIcon(qt.QIcon())
            self.ui.lockButton.setToolTip("")
            return
        if node.GetLocked():
            self.ui.lockButton.setIcon(qt.QIcon(":Icons/Medium/SlicerLock.png"))
            self.ui.lockButton.setToolTip("Target locked. Click to unlock moving the target.")
        else:
            self.ui.lockButton.setIcon(qt.QIcon(":Icons/Medium/SlicerUnlock.png"))
            self.ui.lockButton.setToolTip("Target unlocked. Click to lock target from being moved.")

    def onLockClicked(self):
        node = self.getTargetsListViewCurrentSelection()
        if node is None:
            raise RuntimeError("It should not be possible to click the lock button with no target selected.")
        node.SetLocked(not node.GetLocked())

    def updateApproveButtonEnabled(self):
        if get_openlifu_data_parameter_node().loaded_session is None:
            self.ui.approveButton.setEnabled(False)
            self.ui.approveButton.setToolTip("There is no active session to write the approval")
        else:
            self.ui.approveButton.setEnabled(True)
            self.ui.approveButton.setToolTip("Approve the current transducer position as a virtual fit for the selected target")

    def updateInputOptions(self):
        """Update the algorithm input options"""
        self.algorithm_input_widget.update()
        self.updateVirtualfitButtonEnabled()

    def updateVirtualfitButtonEnabled(self):
        """Update the enabled status of the virtual fit button based on whether all inputs have valid selections"""
        if self.algorithm_input_widget.has_valid_selections():
            self.ui.virtualfitButton.enabled = True
            self.ui.virtualfitButton.setToolTip("Run virtual fit algorithm to automatically suggest a transducer positioning")
        else:
            self.ui.virtualfitButton.enabled = False
            self.ui.virtualfitButton.setToolTip("Specify all required inputs to enable virtual fitting")

    def onApproveClicked(self):
        currently_selected_data = self.algorithm_input_widget.get_current_data()
        self.logic.approve_virtual_fit_for_target(currently_selected_data["Target"])

    def updateApprovalStatusLabel(self):
        data_logic : "OpenLIFUDataLogic" = slicer.util.getModuleLogic('OpenLIFUData')
        if data_logic.validate_session():
            target_id = data_logic.get_virtual_fit_approval_state()
            if target_id is None:
                self.ui.approvalStatusLabel.text = "No virtual fit approval in the session."
            else:
                self.ui.approvalStatusLabel.text = f"Virtual fit approved for \"{target_id}\""
        else:
            self.ui.approvalStatusLabel.text = ""

    def onvirtualfitClicked(self):
        activeData = self.algorithm_input_widget.get_current_data()
        self.logic.virtual_fit(activeData["Protocol"],activeData["Transducer"], activeData["Volume"], activeData["Target"])

#
# OpenLIFUPrePlanningLogic
#


class OpenLIFUPrePlanningLogic(ScriptedLoadableModuleLogic):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return OpenLIFUPrePlanningParameterNode(super().getParameterNode())

    def approve_virtual_fit_for_target(self, target : Optional[vtkMRMLMarkupsFiducialNode] = None):
        """Apply approval for the virtual fit of the given target. If no target is provided, then
        any existing approval is revoked."""
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        session.approve_virtual_fit_for_target(target) # apply the approval or lack thereof
        data_parameter_node.loaded_session = session # remember to write the updated session object into the parameter node

    def revoke_approval_if_any(self, target : vtkMRMLMarkupsFiducialNode):
        """If there was a virtual fit approval for the given target, revoke it.
        It is assumed that the virtual fit approval is being revoked because of a modification to the approved target,
        so an info dialog is raised to that effect.
        If there is no active session then this does nothing.
        """
        data_parameter_node = get_openlifu_data_parameter_node()
        session = data_parameter_node.loaded_session
        if session is None:
            return
        if (
            target is None
            or session.virtual_fit_is_approved_for_target(target)
        ):
            slicer.util.infoDisplay(
                text= "Virtual fit approval has been revoked because the approved target was modified.",
                windowTitle="Approval revoked"
            )
            session.approve_virtual_fit_for_target(None) # revoke approval
            data_parameter_node.loaded_session = session # remember to write the updated session object into the parameter node

    def virtual_fit(
            self,
            protocol: SlicerOpenLIFUProtocol,
            transducer : SlicerOpenLIFUTransducer,
            volume: vtkMRMLScalarVolumeNode,
            target: vtkMRMLMarkupsFiducialNode,
        ):
        # Temporary measure of "manual" virtual fitting. See https://github.com/OpenwaterHealth/SlicerOpenLIFU/issues/153
        transducer.transform_node.CreateDefaultDisplayNodes()
        if not transducer.transform_node.GetDisplayNode().GetEditorVisibility():
            slicer.util.infoDisplay(
                text=(
                    "The automatic virtual fitting algorithm is not yet implemented."
                    " Use the interaction handles on the transducer to manually fit it."
                    " You can click the Virtual fit button again to remove the interaction handles."
                ),
                windowTitle="Not implemented"
            )
            transducer.transform_node.GetDisplayNode().SetEditorVisibility(True)
        else:
            transducer.transform_node.GetDisplayNode().SetEditorVisibility(False)

#
# OpenLIFUPrePlanningTest
#

class OpenLIFUPrePlanningTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
